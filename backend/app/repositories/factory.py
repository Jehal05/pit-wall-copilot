"""Selects the configured repository implementation(s).

`STATE_REPOSITORY` is a single knob covering ALL of the demo's durable
state — race state (Gate 0), and now watches/alerts (Gate 2) too, per the
architecture diagram's own framing of `backend/app/repositories/*` as
"memory | dynamodb — one interface". This used to only be true for race
state; `build_watch_repositories` fixes the previously-documented Gate 2
limitation ("A DynamoDB-backed implementation is not included for
watches/alerts") by giving watches/alerts the same selection.
"""

from __future__ import annotations

from app.config import Settings
from app.repositories.base import RaceStateRepository
from app.repositories.lease import InMemoryLeaseRepository, LeaseRepository
from app.repositories.memory import InMemoryRaceStateRepository
from app.repositories.pairing_base import DeviceTokenRepository, PairingCodeRepository, PairingExchangeCoordinator
from app.repositories.pairing_memory import (
    InMemoryDeviceTokenRepository,
    InMemoryPairingCodeRepository,
    InMemoryPairingExchangeCoordinator,
)
from app.repositories.watch_base import AlertRepository, TransitionWriter, WatchRepository
from app.repositories.watch_memory import InMemoryAlertRepository, InMemoryTransitionWriter, InMemoryWatchRepository


def build_repository(
    settings: Settings, lease_repository: LeaseRepository | None = None
) -> RaceStateRepository:
    """Repair pass: `lease_repository` — normally the SAME instance
    `build_lease_repository` below returns for this same `settings` —
    lets the constructed `RaceStateRepository` verify `LeaseProof`s
    against the authoritative lease record (see
    `app/repositories/lease.py`'s module docstring for why this closes
    the stale-leader window a bare fencing-token floor left open).
    `app/main.py`'s lifespan calls `build_lease_repository` FIRST and
    passes its result here specifically so both point at the same
    underlying lease state. Optional and unused for `memory` mode's own
    bootstrap check below to stay simple for callers (tests, scripts)
    that only need plain race-state storage and have no lease concerns
    at all."""
    if settings.state_repository == "memory":
        # `lease_repository` is accepted but not required to be an
        # `InMemoryLeaseRepository` here for type simplicity; in
        # practice `build_lease_repository` only ever returns one for
        # `state_repository == "memory"`, which is the only case this
        # branch is reached in.
        from app.repositories.lease import InMemoryLeaseRepository

        memory_lease_repository = lease_repository if isinstance(lease_repository, InMemoryLeaseRepository) else None
        return InMemoryRaceStateRepository(lease_repository=memory_lease_repository)

    if settings.state_repository == "dynamodb":
        # Imported lazily so `boto3` is only required when actually selected.
        from app.repositories.dynamodb import DynamoDBRaceStateRepository

        return DynamoDBRaceStateRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
            # Repair pass: "production DynamoDB simulator writes must
            # fail closed without a valid lease proof." STATE_REPOSITORY
            # =dynamodb is exactly the multi-instance-capable deployment
            # target this requirement is about — see
            # DynamoDBRaceStateRepository's own docstring on this flag.
            require_lease_proof=True,
        )

    raise ValueError(
        f"Unknown STATE_REPOSITORY '{settings.state_repository}'. Expected 'memory' or 'dynamodb'."
    )


def build_lease_repository(settings: Settings) -> LeaseRepository:
    """AWS hackathon-final pass, P0-4: distributed simulator leadership.
    Mirrors STATE_REPOSITORY exactly like every other storage decision in
    this module -- "durable/multi-instance or not" is one setting, not
    one per subsystem (see this module's own docstring). A race whose
    state lives in memory (single process, by definition) has no
    cross-instance leadership problem to solve, so `memory` gets the
    trivial in-process lease implementation; `dynamodb` gets the real
    conditional-lease implementation backed by the same table."""
    if settings.state_repository == "memory":
        return InMemoryLeaseRepository()

    if settings.state_repository == "dynamodb":
        from app.repositories.lease import DynamoDBLeaseRepository

        return DynamoDBLeaseRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )

    raise ValueError(
        f"Unknown STATE_REPOSITORY '{settings.state_repository}'. Expected 'memory' or 'dynamodb'."
    )


def build_transition_writer(
    settings: Settings, watch_repository: WatchRepository, alert_repository: AlertRepository
) -> TransitionWriter:
    """Spec v1.1 correction 9: the atomic alert+watch writer WatchEvaluator
    uses for every status-CHANGING evaluation — see TransitionWriter's
    class docstring (watch_base.py) for what this closes relative to the
    old two-step record_transition()+update_watch() sequence. Selected by
    the SAME STATE_REPOSITORY switch as everything else here, and built
    from the exact repository instances the caller already constructed
    (via `build_watch_repositories`) so both stay pointed at the same
    underlying storage."""
    if settings.state_repository == "memory":
        # Both asserts below: internal type-consistency check between objects
        # THIS SAME factory function just constructed together a few lines
        # above (never externally-influenced input); if compiled with -O and
        # stripped, the worst outcome is an AttributeError downstream, not a
        # security bypass.
        assert isinstance(watch_repository, InMemoryWatchRepository)  # nosec B101
        assert isinstance(alert_repository, InMemoryAlertRepository)  # nosec B101
        return InMemoryTransitionWriter(watch_repository, alert_repository)

    if settings.state_repository == "dynamodb":
        from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBTransitionWriter, DynamoDBWatchRepository

        # Both asserts below: same rationale as the memory-backend branch
        # above.
        assert isinstance(watch_repository, DynamoDBWatchRepository)  # nosec B101
        assert isinstance(alert_repository, DynamoDBAlertRepository)  # nosec B101
        # Deliberately NOT built from watch_repository/alert_repository's
        # own boto3 objects — see DynamoDBTransitionWriter's class
        # docstring (watch_dynamodb.py) for why reusing a resource's
        # `.meta.client` for a raw transact_write_items call is a real bug,
        # not just unnecessary. It needs its own plain low-level client.
        return DynamoDBTransitionWriter(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )

    raise ValueError(
        f"Unknown STATE_REPOSITORY '{settings.state_repository}'. Expected 'memory' or 'dynamodb'."
    )


def build_pairing_repositories(settings: Settings) -> tuple[PairingCodeRepository, DeviceTokenRepository]:
    """Spec v1.1 pass 4, mandatory correction 1: selects durable pairing
    storage the same way `build_watch_repositories` selects durable
    watch/alert storage -- via `settings.effective_pairing_repository`,
    which mirrors `STATE_REPOSITORY` unless `PAIRING_REPOSITORY` is set
    explicitly. `STATE_REPOSITORY=dynamodb` (the AWS deployment's setting
    -- see infrastructure/template.yaml) therefore gets durable,
    multi-instance-safe pairing automatically, with no separate opt-in
    required; only `memory` mode (local development) keeps the
    pass-3-era in-process behavior."""
    repository_kind = settings.effective_pairing_repository

    if repository_kind == "memory":
        return InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository()

    if repository_kind == "dynamodb":
        # Imported lazily so `boto3` is only required when actually selected.
        from app.repositories.pairing_dynamodb import DynamoDBDeviceTokenRepository, DynamoDBPairingCodeRepository

        pairing_code_repository: PairingCodeRepository = DynamoDBPairingCodeRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
        device_token_repository: DeviceTokenRepository = DynamoDBDeviceTokenRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
        return pairing_code_repository, device_token_repository

    raise ValueError(
        f"Unknown PAIRING_REPOSITORY/STATE_REPOSITORY '{repository_kind}'. Expected 'memory' or 'dynamodb'."
    )


def build_pairing_exchange_coordinator(
    settings: Settings,
    pairing_code_repository: PairingCodeRepository,
    device_token_repository: DeviceTokenRepository,
) -> PairingExchangeCoordinator:
    """AWS hackathon-final pass, phase 3: builds the atomic pairing
    exchange coordinator (`pairing_base.py`'s `PairingExchangeCoordinator`)
    alongside `build_pairing_repositories`, selected by the SAME
    `effective_pairing_repository` setting.

    Memory mode: constructed directly around the SAME `InMemory*`
    repository instances the caller already built (`pairing_code_repository`/
    `device_token_repository` MUST be the ones returned from
    `build_pairing_repositories` for this same settings object — the
    coordinator manipulates their internal state directly to make the
    combined operation atomic; see `InMemoryPairingExchangeCoordinator`'s
    own docstring), so reads through either repository's normal public
    methods always agree with what the coordinator has committed.

    DynamoDB mode: a fresh, independent low-level client pointed at the
    same table (mirrors `build_transition_writer`'s own documented reason
    for never reusing a `Table` resource's `.meta.client` for a raw
    `transact_write_items` call)."""
    repository_kind = settings.effective_pairing_repository

    if repository_kind == "memory":
        # Both asserts below: internal type-consistency check between objects
        # THIS SAME factory function just constructed together a few lines
        # above (never externally-influenced input); if compiled with -O and
        # stripped, the worst outcome is an AttributeError downstream, not a
        # security bypass.
        assert isinstance(pairing_code_repository, InMemoryPairingCodeRepository)  # nosec B101
        assert isinstance(device_token_repository, InMemoryDeviceTokenRepository)  # nosec B101
        return InMemoryPairingExchangeCoordinator(pairing_code_repository, device_token_repository)

    if repository_kind == "dynamodb":
        from app.repositories.pairing_dynamodb import DynamoDBPairingExchangeCoordinator

        return DynamoDBPairingExchangeCoordinator(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )

    raise ValueError(
        f"Unknown PAIRING_REPOSITORY/STATE_REPOSITORY '{repository_kind}'. Expected 'memory' or 'dynamodb'."
    )


def build_pairing_rate_limiter(settings: Settings, max_attempts: int, window_seconds: float):
    """Spec v1.1 pass 4, mandatory correction 1: "do not depend only on an
    in-process dictionary" for pairing-attempt rate limiting. Mirrors the
    same `effective_pairing_repository` switch: `dynamodb` mode gets a
    distributed, multi-task-safe counter (DynamoDBPairingRateLimiter);
    `memory` mode keeps the pass-3-era in-process
    `PairingAttemptRateLimiter`. See `docs/deployment-runbook.md` for the
    complementary AWS WAF rate-based-rule recommendation for the public
    route at the network edge."""
    from app.services.device_pairing import PairingAttemptRateLimiter

    repository_kind = settings.effective_pairing_repository
    if repository_kind == "memory":
        return PairingAttemptRateLimiter(max_attempts=max_attempts, window_seconds=window_seconds)

    if repository_kind == "dynamodb":
        from app.repositories.pairing_dynamodb import DynamoDBPairingRateLimiter

        return DynamoDBPairingRateLimiter(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            max_attempts=max_attempts,
            window_seconds=window_seconds,
            endpoint_url=settings.dynamodb_endpoint_url,
        )

    raise ValueError(
        f"Unknown PAIRING_REPOSITORY/STATE_REPOSITORY '{repository_kind}'. Expected 'memory' or 'dynamodb'."
    )


def build_watch_repositories(settings: Settings) -> tuple[WatchRepository, AlertRepository]:
    """Same STATE_REPOSITORY switch as `build_repository`, applied to
    watches/alerts. They share the same underlying DynamoDB table as race
    state (see watch_dynamodb.py's module docstring for the sk-prefix
    scheme) rather than requiring a second table to provision."""
    if settings.state_repository == "memory":
        return InMemoryWatchRepository(), InMemoryAlertRepository()

    if settings.state_repository == "dynamodb":
        # Imported lazily so `boto3` is only required when actually selected.
        from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBWatchRepository

        watch_repository: WatchRepository = DynamoDBWatchRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
        alert_repository: AlertRepository = DynamoDBAlertRepository(
            table_name=settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
        return watch_repository, alert_repository

    raise ValueError(
        f"Unknown STATE_REPOSITORY '{settings.state_repository}'. Expected 'memory' or 'dynamodb'."
    )
