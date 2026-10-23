# Operations runbook

AWS hackathon-final pass, phase 9. The full operator-facing runbook —
where to look, every CloudWatch alarm and what to do when it fires
(including the phase 5 additions: `<stack>-event-broker-degraded`,
`<stack>-bedrock-throttled`), the EMF custom metrics, the OTel tracing
pipeline, and structured logging conventions — lives in
**[`observability-runbook.md`](./observability-runbook.md)**;
this file exists only so `OPERATIONS_RUNBOOK.md` resolves at the repo
root for anyone looking for it by that name.

Quick links:
- Full alarm table + response procedures: `observability-runbook.md`
- Deploy/backup/secret-rotation procedures: `DEPLOYMENT.md` (→ `deployment-runbook.md`)
- One-command local verification: `scripts/preflight.sh`
- What each phase-5 observability piece is verified vs. not: `observability-runbook.md`'s own "HONEST STATUS" notes and `FINAL_PRODUCTION_READINESS_REPORT.md` (removed in the repo cleanup; see git history)
