"""Distributed simulator leadership — AWS hackathon-final pass, P0-4.

`SimulatorService._run_loop` runs once per backend PROCESS that had
`start()` called on it (see simulator.py's module docstring). With
`BackendService.DesiredCount` raised above 1 (the scaling story this
template's own comments describe), more than one ECS task can end up with
a live `_run_loop` for the same race — most plausibly a client retrying a
`POST /start` whose response never arrived, with the retry landing on a
different task than the original request. Both loops would then advance
the SAME race independently: wasted DynamoDB write capacity, a thundering
herd of `StaleWriteError`s (already the existing optimistic-concurrency
safety net — see repositories/base.py's module docstring — so DATA
CORRUPTION was never actually possible), and log noise that makes a real
incident harder to diagnose. This module adds an explicit LEADER election
so at most one task is ever the one actually attempting each tick, rather
than relying on "the loser's write gets rejected" as the only defense.

Lease design: one lease per race (`pk="LEASE#{race_id}"`, `sk="LEASE"`),
holding:
  - `owner_id`   — a random id unique to one SimulatorService instance
                   (regenerated on every process start; see
                   simulator.py's `self._instance_id`).
  - `expires_at` — an ISO-8601 timestamp; the lease is up for grabs by
                   anyone once this passes, whether or not the previous
                   owner is still alive.
  - `fencing_token` — a strictly-increasing integer, incremented on every
                   successful acquire (including renewal). A crashed
                   leader that somehow resumes writing after its lease
                   expired carries a fencing token the NEXT leader has
                   already superseded — callers that thread the fencing
                   token through to a write's own conditional check (not
                   required here because save_state/save_control already
                   enforce a stronger per-run version check independently
                   — see repositories/base.py — but exposed for any
                   future caller that needs it) can detect and reject a
                   stale write from an old leader even after the lease
                   itself has moved on.

`try_acquire_or_renew` is the one operation every implementation must
provide, and it must be atomic: either this call is the one that
(re)claims the lease for `owner_id`, or it isn't — there is no
partial/ambiguous outcome visible to the caller.

*** Fixing the stale-leader window (repair pass) ***

An earlier version of this fencing design threaded only a bare
`fencing_token: int` to `RaceStateRepository.save_state`/`save_control`,
which compared it against a monotonically-raised "floor" stored on the
race's own GENERATION item — a floor that is only ever raised by an
ACCEPTED WRITE, not by a lease acquisition. That leaves a real window:

  1. Instance A acquires/renews the lease and is granted fencing_token=5.
  2. A pauses (GC, scheduler preemption, a slow network call) for longer
     than the lease TTL — its lease now sits expired, unrenewed.
  3. Instance B's next bid succeeds (the lease is expired), granting B
     fencing_token=6. B has not yet written race state, so the
     GENERATION item's floor is still whatever A's own last-accepted
     write left it at (say, 4).
  4. A resumes and submits its captured token, 5. The floor check only
     asks "is 5 > the last-accepted floor (4)?" — yes — so A's stale
     write is WRONGLY accepted, even though the LEASE item itself
     already names B, not A, as the current holder.

The floor was comparing a write's token against history (what was ever
accepted), not against the present (who currently, provably, holds the
lease). The fix: `save_state`/`save_control` now take a `LeaseProof`
(below) instead of a bare int, and the persistence layer verifies that
proof against the LIVE lease item — owner, exact token match (not `<=`),
not-expired, correct race — as part of the SAME atomic write (one
`TransactWriteItems` call for DynamoDB; one nested critical section
spanning both this repository's lock and the race-state repository's own
lock for the in-memory test double — see `InMemoryLeaseRepository.matches_locked`
and `InMemoryRaceStateRepository`'s constructor). A's step-4 write above
now fails: by the time A resumes, the LEASE item's `fencing_token` is 6
and its `owner_id` is B, so A's proof (token 5, owner A) no longer
matches the live lease record, regardless of what any floor says.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def simulator_lease_name(race_id: str) -> str:
    """The one canonical place this application's simulator-lease naming
    convention is defined (P0-2 repair). Previously `SimulatorService
    ._lease_name` duplicated this f-string itself; every persistence-layer
    cross-race check (`InMemoryRaceStateRepository`,
    `DynamoDBRaceStateRepository`) also needs to know this exact
    convention to verify "does this lease_proof's lease_name actually
    belong to the race this write targets", so it is defined once, here,
    and imported by both `simulator.py` and the repository modules rather
    than re-derived in each."""
    return f"simulator:{race_id}"


@dataclass(frozen=True)
class LeaseGrant:
    """Returned on a successful acquire/renew. `fencing_token` is
    strictly greater than any token previously issued for this lease
    name, across every owner that has ever held it."""

    owner_id: str
    race_id: str
    fencing_token: int
    expires_at: datetime


@dataclass(frozen=True)
class LeaseProof:
    """A structured, self-describing claim of lease ownership, built
    directly from a `LeaseGrant` a caller actually received from
    `try_acquire_or_renew` — never fabricated ad hoc. Threaded through to
    `RaceStateRepository.save_state`/`save_control` in place of a bare
    `fencing_token: int` (see this module's own docstring for why a bare
    integer left a stale-leader window open): a bare int carries no
    record of WHICH lease, WHICH race, or WHOSE claim it is, so a caller
    (or a bug) could pass an arbitrary large value and have it compared
    only against a historical floor. `LeaseProof` is still an ordinary
    Python value an in-process caller could in principle construct by
    hand, but bundling every field the persistence layer actually checks
    — and always constructing it from a real `LeaseGrant` at the one call
    site that does (`SimulatorService._run_loop`) — makes that misuse
    structurally visible in review rather than an easy one-token typo."""

    lease_name: str
    race_id: str
    owner_id: str
    fencing_token: int

    @classmethod
    def from_grant(cls, lease_name: str, grant: LeaseGrant) -> "LeaseProof":
        return cls(
            lease_name=lease_name,
            race_id=grant.race_id,
            owner_id=grant.owner_id,
            fencing_token=grant.fencing_token,
        )


class LeaseRepository(ABC):
    """Persistence boundary for distributed-leader leases — mirrors
    `RaceStateRepository`'s role for race state (base.py)."""

    @abstractmethod
    async def try_acquire_or_renew(
        self, lease_name: str, race_id: str, owner_id: str, ttl_seconds: float
    ) -> Optional[LeaseGrant]:
        """Attempts to claim or renew `lease_name` for `owner_id`.

        `race_id` is persisted on the lease item itself (repair pass) so
        that a `LeaseProof` built from the returned grant can be verified
        against the SAME record later ("lease name belongs to the
        correct race" — one of the explicit checks the persistence layer
        now makes; see this module's own docstring). For this
        application's one caller, `race_id` is already implied by
        `lease_name`'s own `f"simulator:{race_id}"` convention
        (`SimulatorService._lease_name`) — storing it explicitly, rather
        than parsing it back out of the name, avoids coupling the
        verification path to that naming convention.

        Succeeds (returns a LeaseGrant) when:
          - no lease currently exists for this name (first-ever claim), or
          - the existing lease has expired (owner_id may differ from the
            previous holder — this is how a crashed leader is
            automatically replaced), or
          - the existing lease is CURRENTLY held by this same owner_id
            (renewal — extends expires_at and bumps the fencing token).

        Fails (returns None) when the lease is currently held by a
        DIFFERENT owner_id and has not yet expired. Must never raise for
        ordinary contention — losing a race for the lease is an expected,
        common outcome, not an error.
        """

    @abstractmethod
    async def get_lease(self, lease_name: str) -> Optional[LeaseGrant]:
        """Read-only lookup, for diagnostics/tests — never used to decide
        whether a write may proceed (only `try_acquire_or_renew`'s own
        atomic outcome may decide that)."""


class InMemoryLeaseRepository(LeaseRepository):
    """Single-process/shared-object implementation. AWS hackathon-final
    pass's two-instance leadership test constructs TWO
    `SimulatorService`s that both point at the SAME
    `InMemoryLeaseRepository` instance (exactly as the existing
    reset/state-version tests already share one `InMemoryRaceStateRepository`
    across simulated "instances") — this class's own internal dict +
    lock is what makes that a meaningful stand-in for two ECS tasks
    sharing one DynamoDB table."""

    def __init__(self) -> None:
        self._leases: dict[str, LeaseGrant] = {}
        self._next_token: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire_or_renew(
        self, lease_name: str, race_id: str, owner_id: str, ttl_seconds: float
    ) -> Optional[LeaseGrant]:
        async with self._lock:
            now = _utc_now()
            existing = self._leases.get(lease_name)
            if existing is not None and existing.race_id != race_id:
                # P0-2 repair: a lease NAME must never silently rebind to a
                # different race_id, even for the same owner_id and even
                # once the original grant has expired — the authoritative
                # binding is the lease record's own stored race_id, not
                # who's asking or when. Treated identically to ordinary
                # lost contention (return None, never raise).
                return None
            if existing is not None and existing.owner_id != owner_id and existing.expires_at > now:
                return None  # held by someone else, not yet expired

            token = self._next_token.get(lease_name, 0) + 1
            self._next_token[lease_name] = token
            grant = LeaseGrant(
                owner_id=owner_id,
                race_id=race_id,
                fencing_token=token,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._leases[lease_name] = grant
            return grant

    async def get_lease(self, lease_name: str) -> Optional[LeaseGrant]:
        return self._leases.get(lease_name)

    @property
    def lock(self) -> asyncio.Lock:
        """Exposed so `InMemoryRaceStateRepository` can nest into this
        SAME lock while verifying a `LeaseProof` — see
        `matches_locked`'s own docstring for why nesting (rather than
        reading `get_lease` independently) is what makes the check
        atomic with respect to a concurrent `try_acquire_or_renew`, the
        in-memory equivalent of the single DynamoDB
        `TransactWriteItems` the production repository uses."""
        return self._lock

    def matches_locked(self, lease_name: str, race_id: str, owner_id: str, fencing_token: int) -> bool:
        """True iff `lease_name`'s CURRENT lease record exactly matches
        every field of the claimed proof and has not expired. Must only
        be called while holding `self.lock` — `InMemoryRaceStateRepository`
        does this by nesting `async with self._lease_repository.lock:`
        inside its OWN lock's critical section before committing a
        fenced write, so no concurrent `try_acquire_or_renew` call (which
        also needs this same lock) can slip in between the check and the
        write, exactly mirroring what one atomic DynamoDB transaction
        guarantees for the production repository. Calling this without
        holding the lock would leave the same TOCTOU gap the earlier
        floor-based design had, just moved to a different pair of
        objects — the nested-lock discipline is what actually closes it,
        not this method's own logic in isolation."""
        grant = self._leases.get(lease_name)
        if grant is None:
            return False
        return (
            grant.race_id == race_id
            and grant.owner_id == owner_id
            and grant.fencing_token == fencing_token
            and grant.expires_at > _utc_now()
        )


class DynamoDBLeaseRepository(LeaseRepository):
    """DynamoDB-backed implementation, using the same table as
    `DynamoDBRaceStateRepository` (one table, one deployment's worth of
    provisioning — see dynamodb.py's module docstring for the existing
    pk/sk convention this follows: `pk="LEASE#{lease_name}"`, `sk="LEASE"`).

    Acquire/renew is a single conditional `UpdateItem`:
      - `owner_id`, `fencing_token`, `expires_at_epoch` are all updated
        unconditionally in the SET clause;
      - the ConditionExpression allows the write through when EITHER no
        item exists yet, OR the stored `expires_at_epoch` is in the past,
        OR the stored `owner_id` already equals this caller's own
        `owner_id` (renewal). Anything else (a different, still-live
        owner) fails the condition, and boto3 raises
        `ConditionalCheckFailedException` — caught here and translated to
        `None`, exactly like a losing bid is expected to look.
      - `fencing_token` is computed as `if_not_exists(fencing_token, 0) + 1`
        directly in the UpdateExpression, so the increment itself is part
        of the same atomic conditional write — no read-then-write gap an
        interleaved acquire could exploit.
    """

    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)

    @staticmethod
    def _pk(lease_name: str) -> str:
        return f"LEASE#{lease_name}"

    async def try_acquire_or_renew(
        self, lease_name: str, race_id: str, owner_id: str, ttl_seconds: float
    ) -> Optional[LeaseGrant]:
        from botocore.exceptions import ClientError

        # DynamoDB's boto3 resource API rejects native `float` values
        # (they cannot round-trip losslessly) and requires `Decimal`
        # instead -- see boto3.dynamodb.types.TypeSerializer.
        now = Decimal(str(time.time()))
        expires_at_epoch = now + Decimal(str(ttl_seconds))
        try:
            response = self._table.update_item(
                Key={"pk": self._pk(lease_name), "sk": "LEASE"},
                # `race_id` (repair pass): persisted unconditionally on
                # every acquire/renew so a later `LeaseProof` can be
                # checked against it directly — see the ABC's own
                # docstring for why this isn't instead parsed back out of
                # `lease_name`.
                UpdateExpression=(
                    "SET owner_id = :owner, race_id = :race_id, expires_at_epoch = :expires, "
                    "fencing_token = if_not_exists(fencing_token, :zero) + :one"
                ),
                # P0-2 repair: the trailing `AND (... OR race_id = :race_id)`
                # clause is what stops a lease NAME from silently rebinding
                # to a different race_id — see `simulator_lease_name`'s own
                # docstring and `InMemoryLeaseRepository`'s equivalent
                # check. A mismatch here fails the whole condition, so it
                # takes the same ConditionalCheckFailedException -> None
                # path as ordinary lost contention, uniformly.
                ConditionExpression=(
                    "(attribute_not_exists(pk) OR expires_at_epoch < :now OR owner_id = :owner) "
                    "AND (attribute_not_exists(pk) OR race_id = :race_id)"
                ),
                ExpressionAttributeValues={
                    ":owner": owner_id,
                    ":race_id": race_id,
                    ":expires": expires_at_epoch,
                    ":now": now,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

        item = response["Attributes"]
        return LeaseGrant(
            owner_id=item["owner_id"],
            race_id=item["race_id"],
            fencing_token=int(item["fencing_token"]),
            expires_at=datetime.fromtimestamp(float(item["expires_at_epoch"]), tz=timezone.utc),
        )

    async def get_lease(self, lease_name: str) -> Optional[LeaseGrant]:
        response = self._table.get_item(Key={"pk": self._pk(lease_name), "sk": "LEASE"})
        item = response.get("Item")
        if item is None:
            return None
        return LeaseGrant(
            owner_id=item["owner_id"],
            race_id=item["race_id"],
            fencing_token=int(item["fencing_token"]),
            expires_at=datetime.fromtimestamp(float(item["expires_at_epoch"]), tz=timezone.utc),
        )

    @staticmethod
    def lease_key(lease_name: str) -> dict[str, str]:
        """The raw `{pk, sk}` key for this lease's item — exposed so
        `DynamoDBRaceStateRepository` can build a `ConditionCheck`
        transact item against this SAME item without duplicating the
        `pk="LEASE#{lease_name}"`/`sk="LEASE"` convention in a second
        place (see dynamodb.py's own use of this)."""
        return {"pk": DynamoDBLeaseRepository._pk(lease_name), "sk": "LEASE"}
