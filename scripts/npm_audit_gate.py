#!/usr/bin/env python3
"""npm audit security gate (AWS hackathon-final winning pass, Phase 1;
strengthened in a later correction pass to fail closed on the ways a
gate like this actually gets defeated in practice, not just the happy
path).

Replaces a bare `npm audit --audit-level=high` in CI, which this
project's Fire TV toolchain currently fails outright (7 high + 7
moderate findings, all root-caused to exactly two GHSA advisories in
Metro-bundler/CLI build tooling -- see
`fire-tv/security/npm-audit-policy.json`'s own `exposure_reason` fields
for the honest technical analysis of why those two are build-time-only,
not runtime, exposure). A bare `--audit-level=high` gate is a blunt
instrument: it cannot distinguish "a NEW finding just appeared" from
"the same two pre-existing, already-analyzed, build-time-only findings
are still here," so it can only ever be silenced by weakening it
wholesale or ignoring it. This script is the narrow alternative: it
still fails hard on anything genuinely new, expired, critical, or
runtime-exposed -- it just doesn't re-fail on the same two findings
every single run once a human has actually looked at them and written
down why.

Critically, this gate is written to FAIL CLOSED, not just pass the
known-good baseline. A gate that only knows how to say "yes" to the
inputs its author tested is not a gate. Concretely it refuses to pass
(exit non-zero) on every one of the following, even though each one
would otherwise let a real vulnerability ride through silently:

  * npm audit's own metadata says N > 0 vulnerabilities exist, but this
    script's parser pulled zero advisory records out of the
    `vulnerabilities` object -- a parsing/schema mismatch masquerading
    as "no vulnerabilities" is exactly the kind of silent failure this
    gate exists to prevent.
  * npm changes its `audit --json` schema in a way that drops or
    renames a field this script depends on (`vulnerabilities`,
    `metadata.vulnerabilities.total`, a per-package `via` array) --
    detected explicitly, not inferred from an empty result.
  * a NEW advisory shows up for a package that already has an exception
    on the books -- the exception was written for one specific,
    already-reviewed advisory ID, and matching is done strictly by
    advisory ID, never by package name, so a second, different advisory
    against the same package is just another unapproved finding.
  * a finding has no GHSA advisory ID and no npm source identifier at
    all -- it cannot be matched to any policy exception (there is
    nothing to match), so it fails outright rather than being keyed by
    package name as a stand-in identifier.
  * a listed policy exception's `expires` date has passed.
  * a finding is CRITICAL severity -- never auto-accepted through this
    gate, regardless of any policy entry.
  * a finding's matching policy entry declares `"exposure": "runtime"`
    (only `"build_time"` entries are ever honored).
  * the policy file lists the same advisory ID twice.
  * a policy entry's declared `severity` is LOWER than the severity npm
    itself actually reports for that advisory -- someone editing the
    policy file cannot quietly downgrade a finding's severity to make
    it look more acceptable than it is.
  * a policy entry doesn't match ANY current npm audit finding (it's
    stale -- the vulnerability it was written for is gone, so the
    exception is dead weight that should be removed, not carried
    forward forever) -- unless `--allow-stale-policy-entries` is passed.

Usage:
    cd fire-tv && python3 ../scripts/npm_audit_gate.py \\
        --policy security/npm-audit-policy.json

    # For testing, without invoking npm:
    python3 scripts/npm_audit_gate.py --policy <policy.json> --audit-json <fixture.json>

Exit codes:
    0 — every finding is either absent, or covered by a current,
        unexpired, correctly-classified, build-time-only policy
        exception, and every policy entry is either currently in use or
        explicitly permitted to be stale.
    1 — a finding is new/unapproved, an exception has expired, a
        CRITICAL finding exists (never exempt-able), a finding's
        matching policy entry declares `exposure: runtime`, a finding
        has no advisory identifier at all, a policy entry's declared
        severity understates the real finding's severity, the policy
        file has a duplicate advisory ID, or a policy entry is stale
        (unmatched by any current finding) and staleness was not
        explicitly permitted.
    2 — usage error: `npm audit` itself failed to run/parse, its JSON
        output doesn't match the schema this script depends on
        (missing/renamed field), its metadata reports vulnerabilities
        that this script's parser could not actually extract, or the
        policy file itself is malformed (missing `exceptions` list, a
        duplicate advisory ID, or an entry missing `advisory_id`).
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

_GHSA_RE = re.compile(r"(GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})", re.IGNORECASE)


class AuditGateError(Exception):
    pass


def _run_npm_audit(project_dir: Path) -> dict[str, Any]:
    try:
        # `shell=False` (the default; no shell interpolation possible),
        # argument list is a fixed, hardcoded literal with no interpolated/
        # user-controlled component (`project_dir` only sets `cwd`, it is
        # never part of the command line itself), and "npm" is resolved
        # via the same PATH lookup every other invocation of this tool in
        # this codebase relies on (matching how npm/node/sam/aws etc. are
        # invoked elsewhere in this repository's own scripts and CI) --
        # pinning an absolute path here would only make this script less
        # portable across the different machines/CI runners it runs on,
        # for no real security benefit given the fixed argument list.
        result = subprocess.run(  # nosec B603 B607
            ["npm", "audit", "--json"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditGateError(f"failed to run `npm audit --json` in {project_dir}: {exc}") from exc
    # `npm audit` exits non-zero whenever ANY vulnerability is found --
    # that is expected and NOT itself an error here; only a genuinely
    # unparseable stdout (npm crashed, wrong directory, no
    # package-lock.json) is treated as a hard usage error.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AuditGateError(
            f"`npm audit --json` produced unparseable output (exit code {result.returncode}).\n"
            f"stderr:\n{result.stderr}"
        ) from exc


def _validate_schema(audit_report: dict[str, Any]) -> None:
    """Raises AuditGateError if `audit_report` doesn't have the shape this
    script's parsing depends on. npm has changed its `audit --json`
    schema across major versions before; a silently-missing or renamed
    field must be a hard, loud failure here -- never a quiet "0
    findings" that a downstream `if not findings: pass` would wave
    through."""
    if not isinstance(audit_report, dict):
        raise AuditGateError("npm audit --json output is not a JSON object -- npm's audit schema may have changed")

    if "vulnerabilities" not in audit_report or not isinstance(audit_report["vulnerabilities"], dict):
        raise AuditGateError(
            "npm audit --json output has no top-level 'vulnerabilities' object (schema mismatch) -- "
            "refusing to evaluate a report this script cannot actually parse"
        )

    metadata = audit_report.get("metadata")
    metadata_vulns = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    if not isinstance(metadata_vulns, dict) or "total" not in metadata_vulns:
        raise AuditGateError(
            "npm audit --json output has no 'metadata.vulnerabilities.total' field (schema mismatch) -- "
            "this gate relies on it to cross-check the number of vulnerabilities npm reports against the "
            "number of advisory records it can actually extract"
        )

    for package_name, vuln in audit_report["vulnerabilities"].items():
        if not isinstance(vuln, dict) or "via" not in vuln or not isinstance(vuln["via"], list):
            raise AuditGateError(
                f"npm audit --json entry for package {package_name!r} is missing the expected list-typed "
                "'via' field (schema mismatch) -- refusing to evaluate a report this script cannot actually parse"
            )


def _extract_findings(
    audit_report: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Returns (findings, unidentified).

    `findings` is {advisory_id: {"severity": str, "packages": set[str], "urls": set[str]}}
    -- collapses npm's per-affected-package `vulnerabilities` map down to
    the actual distinct root advisories (a single GHSA advisory in a
    build-tool's own dependency commonly shows up against several
    downstream package names in npm's report; this gate reasons about
    the advisory, which is what a human actually reviews and writes an
    exception for, not each of its transitive re-listings).

    `unidentified` is a list of {"package": str, "severity": str} for
    `via` entries that carry NEITHER a GHSA advisory URL NOR an npm
    `source` identifier -- i.e. there is nothing at all to match against
    a policy entry. These are never silently keyed by package name (a
    package name is not an advisory identifier, and doing so risks an
    accidental/attacker-crafted collision with an unrelated policy
    entry); they are reported back to the caller and always fail the
    gate outright.
    """
    findings: dict[str, dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    for package_name, vuln in audit_report.get("vulnerabilities", {}).items():
        for via in vuln.get("via", []):
            if not isinstance(via, dict):
                continue  # a bare string `via` entry is just a package-name pointer, not its own advisory
            url = via.get("url", "")
            severity = via.get("severity", vuln.get("severity", "unknown"))
            match = _GHSA_RE.search(url)
            source = via.get("source")
            if match:
                advisory_id = match.group(1).upper()
            elif source:
                advisory_id = f"NPM-SOURCE-{source}"
            else:
                unidentified.append({"package": package_name, "severity": severity})
                continue

            entry = findings.setdefault(str(advisory_id), {"severity": "low", "packages": set(), "urls": set()})
            if _severity_rank(severity) > _severity_rank(entry["severity"]):
                entry["severity"] = severity
            entry["packages"].add(package_name)
            if url:
                entry["urls"].add(url)
    return findings, unidentified


_SEVERITY_ORDER = ["info", "low", "moderate", "high", "critical", "unknown"]


def _severity_rank(severity: str) -> int:
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        return _SEVERITY_ORDER.index("unknown")


def _load_policy(policy_path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(policy_path.read_text())
    exceptions = raw.get("exceptions")
    if not isinstance(exceptions, list):
        raise AuditGateError(
            f"policy file {policy_path} has no top-level 'exceptions' list (schema mismatch) -- "
            "refusing to evaluate against a policy this script cannot actually parse"
        )

    by_id: dict[str, dict[str, Any]] = {}
    for entry in exceptions:
        if not isinstance(entry, dict) or "advisory_id" not in entry:
            raise AuditGateError(f"policy file {policy_path} contains an exception entry with no 'advisory_id' field")
        advisory_id = str(entry["advisory_id"]).upper()
        if advisory_id in by_id:
            raise AuditGateError(
                f"policy file {policy_path} contains DUPLICATE exception entries for advisory {advisory_id} -- "
                "each advisory may have at most one policy entry; remove the duplicate before this gate can run"
            )
        by_id[advisory_id] = entry
    return by_id


def evaluate(
    findings: dict[str, dict[str, Any]],
    policy: dict[str, dict[str, Any]],
    *,
    today: datetime.date,
    unidentified: list[dict[str, Any]] | None = None,
    allow_stale_policy_entries: bool = False,
) -> tuple[list[str], list[str]]:
    """Returns (failures, exempted_summary_lines)."""
    failures: list[str] = []
    exempted: list[str] = []
    matched_policy_ids: set[str] = set()

    for item in unidentified or []:
        failures.append(
            f"<no advisory id>: severity={item.get('severity', 'unknown')} ({item.get('package', '?')}) — "
            "finding has no GHSA advisory ID or npm source identifier at all, so it cannot be matched to any "
            "policy exception"
        )

    for advisory_id, finding in sorted(findings.items()):
        severity = finding["severity"]
        packages = ", ".join(sorted(finding["packages"]))

        # Look up the policy entry (if any) FIRST, independent of
        # severity, so a critical finding that happens to have a policy
        # entry still counts as "matched" for staleness purposes below
        # -- it's rejected for being critical, not because the entry is
        # unused.
        entry = policy.get(advisory_id)
        if entry is not None:
            matched_policy_ids.add(advisory_id)

        if severity == "critical":
            failures.append(f"{advisory_id}: CRITICAL severity ({packages}) — never exempt-able through this gate")
            continue

        if entry is None:
            failures.append(
                f"{advisory_id}: severity={severity} ({packages}) — NEW/UNAPPROVED finding, no policy entry "
                "(a policy entry for a different advisory against the same package does not cover this one; "
                "exceptions are matched strictly by advisory ID)"
            )
            continue

        exposure = entry.get("exposure")
        if exposure != "build_time":
            failures.append(
                f"{advisory_id}: severity={severity} ({packages}) — policy entry declares "
                f"exposure={exposure!r}, only 'build_time' entries are ever honored as an exception"
            )
            continue

        declared_severity = entry.get("severity")
        if declared_severity is None:
            failures.append(f"{advisory_id}: policy entry has no `severity` field to validate against the real finding")
            continue
        if _severity_rank(declared_severity) < _severity_rank(severity):
            failures.append(
                f"{advisory_id}: policy entry declares severity={declared_severity!r} but the actual npm "
                f"finding severity is {severity!r} ({packages}) — a policy entry may not understate a "
                "finding's real severity"
            )
            continue

        expires_raw = entry.get("expires", "")
        try:
            expires = datetime.date.fromisoformat(expires_raw)
        except ValueError:
            failures.append(f"{advisory_id}: policy entry has no valid `expires` date ({expires_raw!r})")
            continue
        if expires < today:
            failures.append(f"{advisory_id}: policy exception EXPIRED on {expires.isoformat()} (today: {today.isoformat()})")
            continue

        exempted.append(
            f"{advisory_id}  severity={severity}  package={entry.get('package')}  "
            f"expires={expires.isoformat()}  owner={entry.get('owner')}  ({packages})"
        )

    if not allow_stale_policy_entries:
        stale_ids = sorted(set(policy.keys()) - matched_policy_ids)
        for advisory_id in stale_ids:
            failures.append(
                f"{advisory_id}: STALE policy entry — present in the policy file but does not match any "
                "current npm audit finding; remove it (the vulnerability it was written for is gone) or "
                "pass --allow-stale-policy-entries to permit stale entries"
            )

    return failures, exempted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", type=Path, required=True, help="Path to the npm-audit-policy.json file")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory to run `npm audit --json` in (default: current directory)",
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        help="Read a pre-captured `npm audit --json` output from this file instead of invoking npm "
        "(used by tests, and available for CI to pass an already-captured report)",
    )
    parser.add_argument(
        "--allow-stale-policy-entries",
        action="store_true",
        help="Do not fail when a policy entry doesn't match any current npm audit finding. Default: fail "
        "closed on stale entries, since an exception nobody re-verifies is exactly how a policy file rots "
        "into a blanket ignore.",
    )
    args = parser.parse_args(argv)

    try:
        if args.audit_json is not None:
            audit_report = json.loads(args.audit_json.read_text())
        else:
            audit_report = _run_npm_audit(args.project_dir)
        _validate_schema(audit_report)
        policy = _load_policy(args.policy)

        findings, unidentified = _extract_findings(audit_report)

        metadata_total = audit_report["metadata"]["vulnerabilities"].get("total", 0)
        if metadata_total and not findings and not unidentified:
            raise AuditGateError(
                f"npm audit metadata reports {metadata_total} total vulnerabilities, but this gate extracted "
                "ZERO advisory records from the 'vulnerabilities' object. That is a parsing/schema mismatch, "
                "not \"no vulnerabilities\" -- refusing to pass a report this script cannot actually account for."
            )
    except (AuditGateError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"npm_audit_gate: USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    metadata = audit_report.get("metadata", {}).get("vulnerabilities", {})
    print(f"npm audit reports: {metadata}")
    print(f"Distinct root advisories found: {len(findings)}")
    if unidentified:
        print(f"Findings with NO advisory identifier at all: {len(unidentified)}")

    failures, exempted = evaluate(
        findings,
        policy,
        today=datetime.date.today(),
        unidentified=unidentified,
        allow_stale_policy_entries=args.allow_stale_policy_entries,
    )

    if exempted:
        print("\nApproved, unexpired, build-time-only exceptions in effect:")
        for line in exempted:
            print(f"  - {line}")

    if failures:
        print("\nnpm_audit_gate: FAILED — the following findings are not covered by an approved exception:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("\nnpm_audit_gate: PASSED — every finding is covered by a current, unexpired, build-time-only exception.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
