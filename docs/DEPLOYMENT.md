# Deployment

AWS hackathon-final pass, phase 9. The full deployment procedure —
prerequisites, `sam deploy` parameters, first-run pairing, backup/restore,
and secret rotation — lives in **[`docs/deployment-runbook.md`](docs/deployment-runbook.md)**;
this file exists only so `DEPLOYMENT.md` resolves at the repo root for
anyone looking for it by that name, without duplicating (and risking
drifting out of sync with) the real content.

Quick links:
- Full deploy steps, parameters, and secret rotation: `docs/deployment-runbook.md`
- Operational alarms and what to do when one fires: `OPERATIONS_RUNBOOK.md` (→ `docs/observability-runbook.md`)
- System architecture this deploys: `AWS_ARCHITECTURE.md`
- Security posture of what's deployed: `SECURITY.md`
- One-command local verification before any deploy: `scripts/preflight.sh`

**HONEST STATUS**: the deploy steps in `docs/deployment-runbook.md` have
been validated statically (`cfn-lint`, `sam validate --lint`, the
infrastructure test suite) in this environment, which has no AWS
credentials and no route to a real AWS account — they have NOT been
exercised against a real `sam deploy`. See
`EXTERNAL_RELEASE_CHECKLIST.md` for the exact commands a deployer with
real AWS access should run to close that gap, and
`FINAL_PRODUCTION_READINESS_REPORT.md` for the full source-readiness vs.
externally-verified-deployment-readiness distinction.
