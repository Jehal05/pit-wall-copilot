"""AWS Lambda entry points for Pit Wall Copilot (spec v1.1 repair 12).

Every module in this package is a *deployment adapter*, not new business
logic: each one wires an existing, already-tested service
(`app.services.*`) to a specific AWS event source. This keeps exactly one
implementation of any rule (e.g. watch evaluation, hysteresis, idempotency)
in the codebase regardless of whether it runs inside the long-running
backend process (Gate 0-4, `app/main.py`'s in-process
`on_state_committed` hook) or as a standalone Lambda invoked by AWS (Gate
5 / repair 12's "full AWS deployment" — see
`app/services/watch_evaluator.py`'s own module docstring, which already
documented this exact split before any Lambda code existed).

None of the handlers here have been invoked by real AWS Lambda in this
environment — there is no AWS account available (see
`../../../IMPLEMENTATION_STATUS.md`). They are packaged and referenced by
`infrastructure/template.yaml`, and covered by unit tests
(`tests/lambda_handlers/`) that exercise the handler function directly
with realistic synthetic DynamoDB Streams event payloads and a real
in-memory-backed evaluator — proving the event-parsing and evaluation
wiring, not a real Streams invocation.
"""
