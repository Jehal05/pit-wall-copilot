"""DynamoDB repository implementation.

Table design (documented, not auto-created by the app — provision via
infrastructure/ or the AWS console/CLI):

Table: pit-wall-race-state (configurable via DYNAMODB_TABLE_NAME)
  Partition key: pk   (string)  e.g. "RACE#demo-race-001"
  Sort key:      sk   (string)  e.g. "STATE" | "CONTROL" | "GENERATION"

Items:
  STATE item:      { pk, sk="STATE", race_id, run_id, state_version, data (JSON) }
  CONTROL item:     { pk, sk="CONTROL", race_id, run_id, state_version, data (JSON) }
  GENERATION item:  { pk, sk="GENERATION", active_run_id }

*** Active-run protection (spec v1.1 mandatory correction 1) ***

The pre-correction design used a single ConditionExpression on the STATE
item itself:

    attribute_not_exists(pk) OR run_id <> :run_id OR state_version < :new_version

This was UNSAFE: the `run_id <> :run_id` branch accepts a write for ANY
run_id that differs from whatever is currently stored — including a
DELAYED write from an OLD run arriving after Reset already switched the
race to a NEW run. That old write's run_id differs from the new run's
stored run_id, so the condition is satisfied and the old run resurrects
itself, silently overwriting the active run's state.

The fix separates two concerns the old design conflated into one item's
condition: "which run is allowed to write at all" (governed by the
GENERATION item, changed only by `begin_new_run`) and "is this specific
write newer than the last one from that same run" (governed by the
STATE/CONTROL item's own state_version, as before). Every ordinary write
(`save_state`/`save_control`) is a DynamoDB transaction
(`TransactWriteItems`) with two parts:

  1. A ConditionCheck on the GENERATION item: `active_run_id = :run_id`.
     A write whose run_id is not the currently active one fails HERE,
     regardless of state_version — an old run can never win this check
     again once `begin_new_run` has moved the GENERATION item past it,
     closing the resurrection window completely (there is no gap where
     the GENERATION item is absent or stale: it is written atomically
     alongside the new run's own baseline by `begin_new_run`, never
     deleted).
  2. A conditional Put on the STATE/CONTROL item itself, exactly as
     before (state_version comparison only — the run_id half of the old
     condition is gone, because step 1 already guarantees run_id
     correctness).

The one case with no GENERATION item to check against is bootstrap: a
race that has never had ANY run established. `save_state`/`save_control`
handle that by first attempting the "existing generation" transaction
above; if the ConditionCheck fails AND a follow-up read confirms no
GENERATION item exists at all (as opposed to existing with a different
run_id), a second transaction is attempted that instead CREATES the
GENERATION item (conditioned on `attribute_not_exists(pk)`, so a
concurrent bootstrap race is itself resolved safely — at most one bootstrap
attempt wins) alongside the same STATE/CONTROL put. See `_save_versioned_item`.

`begin_new_run` — the only way the GENERATION item's `active_run_id`
changes — writes the new GENERATION item, the new baseline STATE item and
the new baseline CONTROL item all as ONE transaction, so there is never a
window (unlike the pre-correction design's delete-then-separate-puts
sequence) where the race has no active run, or a stale STATE/CONTROL item
without a matching, already-updated GENERATION item.

*** Lease-proof fencing (repair pass) ***

An earlier version of this module additionally stored a `fencing_token`
attribute on the GENERATION item itself and compared each write's token
against it with a `<=` (monotonic floor) condition. That floor is only
ever raised by an ACCEPTED WRITE — not by a lease acquisition — so a
leader that wins a lease bid but pauses before writing can resume and
still pass the floor check even after a NEWER leader has since taken the
lease. See `app/repositories/lease.py`'s module docstring for the full
attack sequence and why an inequality check against write history cannot
close it.

The fix: `save_state`/`save_control` now accept a `LeaseProof` (built by
`SimulatorService` directly from a `LeaseGrant` it actually received —
see `lease.py`) instead of a bare integer, and — when one is supplied —
this module adds a THIRD `ConditionCheck` transact item, against the
LEASE item itself (`DynamoDBLeaseRepository.lease_key`, same table), to
the SAME `TransactWriteItems` call the GENERATION check and the STATE/
CONTROL put already use:

    attribute_exists(pk)
    AND race_id = :race_id
    AND owner_id = :owner_id
    AND fencing_token = :fencing_token     -- EXACT match, not <=
    AND expires_at_epoch > :now

All three checks (GENERATION active-run, LEASE liveness, STATE/CONTROL
version) either all pass or the whole transaction is cancelled — there is
no window between "check the lease" and "commit the write" for a newer
leader's acquisition to land in, because DynamoDB evaluates every
ConditionCheck/Put in a `TransactWriteItems` call against the SAME
consistent snapshot, atomically. The GENERATION item no longer carries
any fencing_token attribute at all — the lease item is now the single
authoritative source for "who currently holds token N," which is exactly
what closes the stale-leader window.

This module is only imported when STATE_REPOSITORY=dynamodb, so boto3 is an
optional dependency at runtime for local/memory-mode development and tests.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional

from app.domain.models import DemoControlState, RaceState, SimulatorStatus, utc_now
from app.repositories.base import InvalidControlTransitionError, RaceStateRepository, StaleWriteError
from app.repositories.lease import DynamoDBLeaseRepository, LeaseProof, simulator_lease_name


def _race_pk(race_id: str) -> str:
    return f"RACE#{race_id}"


class DynamoDBRaceStateRepository(RaceStateRepository):
    def __init__(
        self,
        table_name: str,
        region_name: str,
        endpoint_url: Optional[str] = None,
        *,
        require_lease_proof: bool = False,
    ) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        self._table_name = table_name
        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)
        # The low-level client is needed for transact_write_items — the
        # higher-level Table resource has no transaction support.
        self._client = session.client("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        # Repair pass: "production DynamoDB simulator writes must fail
        # closed without a valid lease proof." `app/repositories/factory.py`
        # sets this True whenever STATE_REPOSITORY=dynamodb (this
        # repository is only ever constructed there — see that module),
        # so in a real deployment save_state/save_control refuse
        # (StaleWriteError, before even attempting a transaction) any
        # call with no lease_proof, rather than relying solely on
        # SimulatorService's own wiring to always supply one. Defaults
        # False so a test or script that constructs this repository
        # directly, for a purpose that has nothing to do with the
        # simulator (e.g. exercising begin_new_run's own bootstrap path)
        # is not forced to fabricate a lease proof it has no use for.
        self._require_lease_proof = require_lease_proof

    # -- internal helpers ---------------------------------------------

    def _generation_key(self, race_id: str) -> dict[str, Any]:
        return {"pk": _race_pk(race_id), "sk": "GENERATION"}

    async def _get_active_run_id(self, race_id: str) -> Optional[str]:
        response = self._table.get_item(Key=self._generation_key(race_id))
        item = response.get("Item")
        return item["active_run_id"] if item else None

    async def _save_versioned_item(
        self,
        *,
        race_id: str,
        run_id: str,
        item: dict[str, Any],
        version_condition: str,
        version_value: int,
        description: str,
        lease_proof: Optional[LeaseProof] = None,
        expected_control_revision: Optional[int] = None,
    ) -> None:
        """Shared transactional write path for save_state/save_control —
        see this module's own docstring for the full protocol. `item` is a
        plain (non-serialized) dict; the low-level `transact_write_items`
        client call requires already-serialized DynamoDB AttributeValue
        dicts (unlike the higher-level Table resource's put_item, which
        serializes for you), so this method builds those explicitly via
        `_serialize_item`/`_serialize_values` before calling it.

        `lease_proof` (repair pass, persistence-level fencing): when
        provided, a THIRD transact item — a `ConditionCheck` against the
        live LEASE item — joins the GENERATION check and the STATE/
        CONTROL put in the SAME `TransactWriteItems` call, requiring the
        lease to exist, name this exact race_id, be owned by
        `lease_proof.owner_id`, carry a `fencing_token` that EXACTLY
        equals `lease_proof.fencing_token` (not merely `<=`), and not be
        expired as of this call. This is enforced independently of, and
        in addition to, the state_version check below — a stale leader
        whose captured state_version happens to still look valid is still
        rejected here because the LIVE lease no longer matches its proof.
        `lease_proof=None` (the default) omits this transact item
        entirely — unfenced callers (no LeaseRepository configured) are
        unaffected, UNLESS `self._require_lease_proof` is set, in which
        case a missing proof is rejected up front (see `__init__`).

        `expected_control_revision` (production-fix pass, closes the
        "valid leader overwrites an operator pause" race): when provided
        (only ever by `save_control_from_simulator`), the Put's own
        ConditionExpression additionally requires the CONTROL item's
        currently stored `control_revision` to exactly equal this value,
        checked atomically as part of the SAME transaction as everything
        else here. A mismatch means an operator call
        (`transition_control_from_operator`) has committed since the
        caller of this method last read the control record it built its
        write from — a stale READ, not a stale LEASE — and is reported as
        the same `StaleWriteError` every other fencing rejection here
        produces.
        """
        from botocore.exceptions import ClientError

        if lease_proof is None and self._require_lease_proof:
            raise StaleWriteError(
                f"{description} write for race {race_id!r} run {run_id!r} was rejected: this "
                "repository requires a valid lease_proof (require_lease_proof=True) and none was "
                "supplied — a production DynamoDB simulator write must fail closed, not proceed "
                "unfenced"
            )

        if lease_proof is not None:
            # P0-2 repair: reject a cross-race proof BEFORE any DynamoDB
            # round-trip, deterministically and unconditionally — a proof's
            # own `race_id`/`lease_name` must match THIS write's actual
            # target `race_id`, never merely be internally self-consistent.
            # This closes the exploit where a proof legitimately acquired
            # for a DIFFERENT race was wrongly accepted because the LEASE
            # ConditionCheck below used to compare `lease_proof.race_id`
            # against itself instead of against this write's real target.
            if lease_proof.race_id != race_id:
                raise StaleWriteError(
                    f"lease_proof race_id {lease_proof.race_id!r} does not match this write's "
                    f"actual target race {race_id!r} — a lease proof acquired for one race can "
                    "never authorize a write to another race"
                )
            expected_lease_name = simulator_lease_name(race_id)
            if lease_proof.lease_name != expected_lease_name:
                raise StaleWriteError(
                    f"lease_proof lease_name {lease_proof.lease_name!r} does not match the "
                    f"expected convention {expected_lease_name!r} for race {race_id!r}"
                )

        generation_key = self._generation_key(race_id)

        def attempt(*, generation_put_new: bool) -> None:
            # Item order is significant: CancellationReasons (below) is
            # positional and must be interpreted against this SAME order.
            transact_items: list[dict[str, Any]] = []

            if generation_put_new:
                # Bootstrap path: no GENERATION item exists yet — CREATE it
                # as part of this same transaction (conditioned so a
                # concurrent bootstrap race can only let one writer win).
                generation_item: dict[str, Any] = {**generation_key, "active_run_id": run_id}
                transact_items.append(
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize_item(generation_item),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                )
            else:
                # The GENERATION item must already say this exact run_id
                # is active, or this whole transaction fails — this is
                # what makes an old/superseded run's write impossible to
                # accept, at any state_version. (No longer carries any
                # fencing_token attribute of its own — see this method's
                # docstring for why that floor-based design was replaced.)
                transact_items.append(
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize_item(generation_key),
                            "ConditionExpression": "active_run_id = :run_id",
                            "ExpressionAttributeValues": _serialize_values({":run_id": run_id}),
                        }
                    }
                )

            if lease_proof is not None:
                # Repair pass: the authoritative check. `:now` is
                # evaluated at the moment this transaction is SUBMITTED —
                # the same "client clock, small skew tolerable" pattern
                # DynamoDBLeaseRepository.try_acquire_or_renew already
                # uses for its own expiry comparisons.
                now = Decimal(str(time.time()))
                transact_items.append(
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize_item(DynamoDBLeaseRepository.lease_key(lease_proof.lease_name)),
                            "ConditionExpression": (
                                "attribute_exists(pk) AND race_id = :lease_race_id AND "
                                "owner_id = :lease_owner_id AND fencing_token = :lease_fencing_token AND "
                                "expires_at_epoch > :lease_now"
                            ),
                            "ExpressionAttributeValues": _serialize_values(
                                {
                                    # Defense-in-depth: `race_id` (this
                                    # write's ACTUAL target), not
                                    # `lease_proof.race_id` — guaranteed
                                    # equal by the pre-check above by the
                                    # time we reach here, but using the
                                    # real target directly is what makes
                                    # this check meaningful even if that
                                    # pre-check were ever bypassed or
                                    # weakened (P0-2 repair).
                                    ":lease_race_id": race_id,
                                    ":lease_owner_id": lease_proof.owner_id,
                                    ":lease_fencing_token": lease_proof.fencing_token,
                                    ":lease_now": now,
                                }
                            ),
                        }
                    }
                )

            put_values: dict[str, Any] = {":new_version": version_value}
            item_condition = version_condition
            if expected_control_revision is not None:
                # Production-fix pass: AND'd onto the version condition
                # (not OR'd — a control_revision mismatch must reject the
                # write even when the version check alone would have
                # allowed it), and skipped entirely on the bootstrap path
                # (`attribute_not_exists(pk)` — there is no prior revision
                # to compare against on a brand-new item).
                item_condition = f"({version_condition}) AND control_revision = :expected_control_revision"
                put_values[":expected_control_revision"] = expected_control_revision
            transact_items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize_item(item),
                        "ConditionExpression": f"attribute_not_exists(pk) OR {item_condition}",
                        "ExpressionAttributeValues": _serialize_values(put_values),
                    }
                }
            )
            self._client.transact_write_items(TransactItems=transact_items)

        # Index of the GENERATION check's own CancellationReasons entry is
        # always 0 (it is always the first item appended above, in both
        # branches) — used below to disambiguate "GENERATION failed" from
        # "LEASE or item failed" without depending on the total item count.
        generation_reason_index = 0

        try:
            attempt(generation_put_new=False)
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = exc.response.get("CancellationReasons", [])
            generation_reason = reasons[generation_reason_index] if len(reasons) > generation_reason_index else {}

            if generation_reason.get("Code") != "ConditionalCheckFailed":
                # The GENERATION check passed — so this is either a
                # genuine stale/duplicate write within the already-active
                # run (the item put's own version condition failed), or
                # (repair pass) the LEASE ConditionCheck failed because
                # the live lease no longer matches this write's proof.
                # Both are "this write is stale, reject it" from the
                # caller's point of view.
                raise StaleWriteError(
                    f"Rejected stale/duplicate {description} write for race {race_id} run {run_id} "
                    f"(lease_proof={lease_proof!r})"
                ) from exc

            # The GENERATION check failed. Disambiguate bootstrap (no
            # GENERATION item exists at all) from a real mismatch (a
            # DIFFERENT run_id is active) with a fresh, consistent read —
            # the ConditionCheck failure code alone cannot tell these
            # apart (DynamoDB reports both as ConditionalCheckFailed).
            active_run_id = await self._get_active_run_id(race_id)
            if active_run_id is not None:
                raise StaleWriteError(
                    f"run_id {run_id!r} is not the active run ({active_run_id!r}) for "
                    f"race {race_id!r} — only begin_new_run may change the active run"
                ) from exc

        # Bootstrap: no GENERATION item exists. Retry, this time creating
        # the GENERATION item as part of the same transaction rather than
        # checking it.
        try:
            attempt(generation_put_new=True)
        except ClientError as exc2:
            if exc2.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = exc2.response.get("CancellationReasons", [])
            generation_reason = reasons[generation_reason_index] if len(reasons) > generation_reason_index else {}
            if generation_reason.get("Code") == "ConditionalCheckFailed":
                # Lost the bootstrap race to a concurrent writer — someone
                # else's run_id is now active. Re-check and reject
                # accordingly (this is a real, if rare, StaleWriteError,
                # not a bug: two concurrent "first ever" writers can only
                # let one of them become the active run).
                active_run_id = await self._get_active_run_id(race_id)
                raise StaleWriteError(
                    f"run_id {run_id!r} lost the race to establish the first active run for "
                    f"race {race_id!r} (active run is now {active_run_id!r})"
                ) from exc2
            raise StaleWriteError(
                f"Rejected stale/duplicate {description} write for race {race_id} run {run_id} "
                f"(lease_proof={lease_proof!r})"
            ) from exc2

    # -- public interface ------------------------------------------------

    async def get_state(self, race_id: str) -> Optional[RaceState]:
        response = self._table.get_item(Key={"pk": _race_pk(race_id), "sk": "STATE"})
        item = response.get("Item")
        if not item:
            return None
        return RaceState.model_validate_json(item["data"])

    async def save_state_from_simulator(
        self, state: RaceState, *, lease_proof: Optional[LeaseProof] = None
    ) -> RaceState:
        item = {
            "pk": _race_pk(state.race_id),
            "sk": "STATE",
            "race_id": state.race_id,
            "run_id": state.run_id,
            "state_version": state.state_version,
            "data": state.model_dump_json(),
        }
        await self._save_versioned_item(
            race_id=state.race_id,
            run_id=state.run_id,
            item=item,
            version_condition="state_version < :new_version",  # strictly increasing
            version_value=state.state_version,
            description="state",
            lease_proof=lease_proof,
        )
        return state

    async def get_control(self, race_id: str) -> Optional[DemoControlState]:
        # Production-fix pass: CONTROL reads are always strongly
        # consistent. Control state is a single, low-QPS item (never the
        # high-frequency per-tick race-state reads `get_state` serves),
        # and it is the authoritative source for both operator-facing
        # status (`/status`, and the response returned immediately after
        # `/start`/`/pause`) and for the revision a simulator write fences
        # itself against — an eventually-consistent read here could hand
        # back a status/control_revision that is already stale by the
        # time the caller acts on it, silently reopening exactly the
        # windows `control_revision` fencing exists to close. A single
        # GetItem's cost difference between eventual and strong
        # consistency is negligible at this item's size/QPS, so there is
        # no reason to take the weaker guarantee here.
        response = self._table.get_item(
            Key={"pk": _race_pk(race_id), "sk": "CONTROL"}, ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            return None
        return DemoControlState.model_validate_json(item["data"])

    async def save_control_from_simulator(
        self, control: DemoControlState, *, lease_proof: Optional[LeaseProof] = None
    ) -> DemoControlState:
        item = {
            "pk": _race_pk(control.race_id),
            "sk": "CONTROL",
            "race_id": control.race_id,
            "run_id": control.run_id,
            "state_version": control.state_version,
            # `status`/`control_revision` are duplicated here as top-level
            # attributes (not just inside `data`) so `transition_control_
            # from_operator`'s ConditionExpression can reference them —
            # kept in sync with whatever `control` already carries. This
            # method NEVER computes a new `control_revision`: it is taken
            # verbatim from `control.control_revision`, which the
            # simulator tick loop always carries forward unchanged from a
            # prior `get_control()` read (`existing.model_copy(update=...)`
            # never touches this field) — see base.py's docstring.
            "status": control.status.value,
            "control_revision": control.control_revision,
            "current_lap": control.current_lap,
            "data": control.model_dump_json(),
        }
        await self._save_versioned_item(
            race_id=control.race_id,
            run_id=control.run_id,
            item=item,
            version_condition="state_version <= :new_version",  # non-regression only, see base.py
            version_value=control.state_version,
            description="control",
            lease_proof=lease_proof,
            # Production-fix pass: fence this write against the CURRENTLY
            # stored control_revision, not just the lease/state_version —
            # closes the "valid leader overwrites an operator pause" race
            # (see base.py's docstring and _save_versioned_item's own).
            expected_control_revision=control.control_revision,
        )
        return control

    async def transition_control_from_operator(
        self,
        race_id: str,
        run_id: str,
        *,
        allowed_current_statuses: frozenset[SimulatorStatus],
        new_status: SimulatorStatus,
        current_lap: Optional[int] = None,
    ) -> DemoControlState:
        """Production-fix pass: rewritten as a genuine compare-and-set
        with a bounded retry loop, closing the top-level/JSON
        `control_revision` mismatch the prior version had (it computed
        the top-level attribute's new value via DynamoDB's own atomic
        `if_not_exists(...) + 1` SET-clause expression, while separately
        serializing a `data` JSON blob built from THIS CALL'S OWN
        pre-write read of `control_revision` — the two values were never
        actually the same write, so `get_control()`, which deserializes
        from `data`, could report a revision one behind the top-level
        attribute).

        Each attempt: read the control record (strongly consistent, via
        `get_control`), check the current status is allowed, compute
        `next_revision = expected_revision + 1` exactly once, and write
        that SAME literal value everywhere (top-level attribute, `data`
        JSON, and the returned `DemoControlState`) conditioned on the
        stored revision still equalling `expected_revision`. On losing
        the race to a concurrent operator call, retry against a freshly
        re-read revision rather than erroring out — this is what makes
        two genuinely concurrent `/start` (or `/pause`) calls both
        complete successfully without an HTTP 500, at the cost of
        `control_revision` legitimately advancing by more than one across
        the two calls (never regressing, never duplicated — see base.py).
        """
        from botocore.exceptions import ClientError

        pk = _race_pk(race_id)
        generation_key = self._generation_key(race_id)
        allowed_list = sorted(status.value for status in allowed_current_statuses)
        status_placeholders = {f":s{i}": value for i, value in enumerate(allowed_list)}

        max_attempts = 10
        for attempt_number in range(1, max_attempts + 1):
            existing = await self.get_control(race_id)
            if existing is None:
                raise StaleWriteError(
                    f"transition_control_from_operator: no control record exists yet for race "
                    f"{race_id!r} — the race must be initialized first"
                )
            if existing.status not in allowed_current_statuses:
                raise InvalidControlTransitionError(
                    f"operator transition of race {race_id!r} to {new_status.value!r} rejected: "
                    f"current stored status {existing.status.value!r} is not one of "
                    f"{allowed_list}"
                )

            expected_revision = existing.control_revision
            next_revision = expected_revision + 1
            updated_current_lap = current_lap if current_lap is not None else existing.current_lap
            # `next_revision` computed exactly once above, then written
            # verbatim to every representation below — this is the actual
            # fix for the top-level/JSON mismatch: there is only ever one
            # source of truth for what the new revision is.
            provisional = existing.model_copy(
                update={
                    "run_id": run_id,
                    "status": new_status,
                    "current_lap": updated_current_lap,
                    "control_revision": next_revision,
                    "updated_at": utc_now(),
                }
            )

            condition_expression = (
                "run_id = :run_id AND #status IN (" + ", ".join(status_placeholders.keys()) + ") "
                "AND control_revision = :expected_revision"
            )

            transact_items: list[dict[str, Any]] = [
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": _serialize_item(generation_key),
                        "ConditionExpression": "active_run_id = :run_id",
                        "ExpressionAttributeValues": _serialize_values({":run_id": run_id}),
                    }
                },
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_item({"pk": pk, "sk": "CONTROL"}),
                        "UpdateExpression": (
                            "SET #status = :new_status, current_lap = :current_lap, #data = :data, "
                            "control_revision = :next_revision"
                        ),
                        "ConditionExpression": condition_expression,
                        "ExpressionAttributeNames": {"#status": "status", "#data": "data"},
                        "ExpressionAttributeValues": _serialize_values(
                            {
                                ":run_id": run_id,
                                ":expected_revision": expected_revision,
                                ":next_revision": next_revision,
                                ":new_status": new_status.value,
                                ":current_lap": updated_current_lap,
                                ":data": provisional.model_dump_json(),
                                **status_placeholders,
                            }
                        ),
                    }
                },
            ]

            # Item order is significant (mirrors `_save_versioned_item`):
            # GENERATION check is always index 0.
            generation_reason_index = 0
            try:
                self._client.transact_write_items(TransactItems=transact_items)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                reasons = exc.response.get("CancellationReasons", [])
                generation_reason = (
                    reasons[generation_reason_index] if len(reasons) > generation_reason_index else {}
                )
                if generation_reason.get("Code") == "ConditionalCheckFailed":
                    raise StaleWriteError(
                        f"run_id {run_id!r} is not the active run for race {race_id!r} — operator "
                        "transition rejected"
                    ) from exc
                # GENERATION passed; the CONTROL item's own condition
                # (run_id match, status membership, or control_revision
                # equality) failed. Since `existing.status` was already
                # confirmed to be in `allowed_current_statuses` above and
                # `run_id` doesn't change between attempts, the only thing
                # that can have changed is `control_revision` — a
                # concurrent operator call committed between our read and
                # our write. Retry against a fresh read rather than
                # erroring out (this is what makes two genuinely
                # concurrent /start or /pause calls both succeed).
                continue

            # No re-read needed: `provisional` carries exactly what was
            # just written (status, current_lap, control_revision, data)
            # — the whole point of computing `next_revision` once in
            # Python and writing that literal value everywhere, rather
            # than trusting a separately-computed atomic increment.
            return provisional

        raise StaleWriteError(
            f"transition_control_from_operator: exceeded {max_attempts} compare-and-set attempts "
            f"for race {race_id!r} run {run_id!r} — persistent contention from concurrent operator "
            "calls; this should be exceedingly rare outside of adversarial test conditions"
        )

    async def begin_new_run(self, race_id: str, state: RaceState, control: DemoControlState) -> None:
        if state.run_id != control.run_id:
            raise ValueError("begin_new_run requires state.run_id == control.run_id")
        active_run_id = await self._get_active_run_id(race_id)
        if active_run_id == state.run_id:
            raise ValueError(
                f"begin_new_run must establish a NEW run_id for race {race_id!r}, "
                f"got the already-active run_id {state.run_id!r}"
            )
        pk = _race_pk(race_id)
        generation_item = {"pk": pk, "sk": "GENERATION", "active_run_id": state.run_id}
        state_item = {
            "pk": pk,
            "sk": "STATE",
            "race_id": state.race_id,
            "run_id": state.run_id,
            "state_version": state.state_version,
            "data": state.model_dump_json(),
        }
        control_item = {
            "pk": pk,
            "sk": "CONTROL",
            "race_id": control.race_id,
            "run_id": control.run_id,
            "state_version": control.state_version,
            # First-class attributes for `transition_control_from_operator`'s
            # ConditionExpression — see save_control_from_simulator's own
            # comment. A fresh run's baseline naturally resets
            # control_revision to whatever the caller's `control` object
            # carries (0 for `SimulatorService._begin_new_run`'s brand-new
            # DemoControlState — a fresh run reasonably starts its operator
            # revision counter over).
            "status": control.status.value,
            "control_revision": control.control_revision,
            "current_lap": control.current_lap,
            "data": control.model_dump_json(),
        }
        # One transaction: the GENERATION switch and both baseline items
        # land together, or none of them do. No condition is placed on the
        # GENERATION put itself — concurrent resets racing each other is
        # allowed to be last-write-wins (spec v1.1 correction 1 only
        # requires that an OLD run can never resurrect once ANY reset has
        # superseded it, not that concurrent resets are serialized against
        # each other).
        self._client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": self._table_name, "Item": _serialize_item(generation_item)}},
                {"Put": {"TableName": self._table_name, "Item": _serialize_item(state_item)}},
                {"Put": {"TableName": self._table_name, "Item": _serialize_item(control_item)}},
            ]
        )


def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _serialize_values(values: dict[str, Any]) -> dict[str, Any]:
    return _serialize_item(values)
