"""Unit tests for scripts/npm_audit_gate.py (AWS hackathon-final winning
pass, Phase 1: "resolve the npm audit gate honestly" — a narrow,
expiring exception policy, not a blanket ignore).
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from npm_audit_gate import (  # noqa: E402
    AuditGateError,
    _extract_findings,
    _load_policy,
    _validate_schema,
    evaluate,
)

TODAY = datetime.date(2026, 6, 1)


def _audit_report(findings: list[dict]) -> dict:
    """Builds a minimal `npm audit --json`-shaped report from a list of
    {package, severity, url} dicts — enough structure for
    `_extract_findings` to exercise its real parsing path."""
    vulnerabilities = {}
    for f in findings:
        vulnerabilities[f["package"]] = {
            "severity": f["severity"],
            "via": [{"source": 1, "name": f["package"], "severity": f["severity"], "url": f["url"]}],
        }
    return {"vulnerabilities": vulnerabilities, "metadata": {"vulnerabilities": {"total": len(findings)}}}


def _policy_entry(advisory_id: str, *, exposure: str = "build_time", expires: str = "2026-12-31") -> dict:
    return {
        "advisory_id": advisory_id,
        "package": "some-pkg",
        "severity": "high",
        "exposure": exposure,
        "expires": expires,
        "owner": "test-owner",
    }


class TestExtractFindings:
    def test_collapses_the_same_advisory_across_multiple_affected_packages_into_one_finding(self):
        report = _audit_report(
            [
                {"package": "metro", "severity": "high", "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"},
                {
                    "package": "metro-config",
                    "severity": "high",
                    "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                },
            ]
        )
        findings, unidentified = _extract_findings(report)
        assert len(findings) == 1
        assert findings["GHSA-AAAA-BBBB-CCCC"]["packages"] == {"metro", "metro-config"}
        assert unidentified == []

    def test_two_distinct_advisories_are_kept_separate(self):
        report = _audit_report(
            [
                {"package": "a", "severity": "moderate", "url": "https://github.com/advisories/GHSA-1111-2222-3333"},
                {"package": "b", "severity": "high", "url": "https://github.com/advisories/GHSA-4444-5555-6666"},
            ]
        )
        findings, unidentified = _extract_findings(report)
        assert len(findings) == 2
        assert unidentified == []

    def test_a_via_entry_with_no_url_and_no_source_is_reported_as_unidentified_not_keyed_by_package_name(self):
        report = {
            "vulnerabilities": {
                "some-pkg": {
                    "severity": "high",
                    "via": [{"name": "some-pkg", "severity": "high"}],
                }
            },
            "metadata": {"vulnerabilities": {"total": 1}},
        }
        findings, unidentified = _extract_findings(report)
        assert findings == {}
        assert unidentified == [{"package": "some-pkg", "severity": "high"}]


class TestEvaluate:
    def test_a_finding_covered_by_an_unexpired_build_time_exception_passes(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"metro"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert failures == []
        assert len(exempted) == 1

    def test_a_finding_with_no_policy_entry_at_all_fails_as_new_unapproved(self):
        findings = {"GHSA-NEW1-NEW1-NEW1": {"severity": "high", "packages": {"foo"}, "urls": set()}}
        failures, exempted = evaluate(findings, policy={}, today=TODAY)
        assert len(failures) == 1
        assert "NEW/UNAPPROVED" in failures[0]

    def test_an_expired_policy_exception_fails_even_though_it_is_listed(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"metro"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC", expires="2020-01-01")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "EXPIRED" in failures[0]

    def test_a_critical_finding_fails_even_with_a_matching_policy_entry(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "critical", "packages": {"metro"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "CRITICAL" in failures[0]

    def test_a_policy_entry_declaring_runtime_exposure_does_not_exempt_the_finding(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"some-runtime-pkg"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC", exposure="runtime")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "runtime" in failures[0]

    def test_an_exception_expiring_exactly_today_still_passes(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"metro"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC", expires=TODAY.isoformat())}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert failures == []

    def test_an_exception_expiring_yesterday_fails(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"metro"}, "urls": set()}}
        yesterday = (TODAY - datetime.timedelta(days=1)).isoformat()
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC", expires=yesterday)}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1

    def test_multiple_findings_are_evaluated_independently(self):
        findings = {
            "GHSA-GOOD-GOOD-GOOD": {"severity": "high", "packages": {"a"}, "urls": set()},
            "GHSA-BAD1-BAD1-BAD1": {"severity": "high", "packages": {"b"}, "urls": set()},
        }
        policy = {"GHSA-GOOD-GOOD-GOOD": _policy_entry("GHSA-GOOD-GOOD-GOOD")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert len(exempted) == 1

    def test_a_new_advisory_for_a_package_with_an_existing_exception_for_a_different_advisory_is_not_covered(self):
        # Same package ("shared-pkg") already has an approved exception for
        # GHSA-OLD1-OLD1-OLD1. A brand new, different advisory against the
        # SAME package must not silently ride on that exception -- matching
        # is strictly by advisory ID.
        findings = {
            "GHSA-OLD1-OLD1-OLD1": {"severity": "high", "packages": {"shared-pkg"}, "urls": set()},
            "GHSA-NEW2-NEW2-NEW2": {"severity": "high", "packages": {"shared-pkg"}, "urls": set()},
        }
        policy = {"GHSA-OLD1-OLD1-OLD1": _policy_entry("GHSA-OLD1-OLD1-OLD1")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(exempted) == 1
        assert len(failures) == 1
        assert "GHSA-NEW2-NEW2-NEW2" in failures[0]
        assert "NEW/UNAPPROVED" in failures[0]

    def test_a_finding_with_no_advisory_identifier_at_all_always_fails(self):
        findings: dict = {}
        policy: dict = {}
        unidentified = [{"package": "mystery-pkg", "severity": "high"}]
        failures, exempted = evaluate(findings, policy, today=TODAY, unidentified=unidentified)
        assert len(failures) == 1
        assert "no advisory id" in failures[0]
        assert "mystery-pkg" in failures[0]

    def test_a_policy_entry_whose_declared_severity_understates_the_real_finding_fails(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "high", "packages": {"metro"}, "urls": set()}}
        entry = _policy_entry("GHSA-AAAA-BBBB-CCCC")
        entry["severity"] = "moderate"  # downgraded from the real "high" severity
        policy = {"GHSA-AAAA-BBBB-CCCC": entry}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "understate" in failures[0]

    def test_a_policy_entry_whose_declared_severity_overstates_the_real_finding_still_passes(self):
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "moderate", "packages": {"metro"}, "urls": set()}}
        entry = _policy_entry("GHSA-AAAA-BBBB-CCCC")
        entry["severity"] = "high"  # overstated relative to the real "moderate" finding -- not a bypass
        policy = {"GHSA-AAAA-BBBB-CCCC": entry}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert failures == []
        assert len(exempted) == 1

    def test_a_stale_policy_entry_that_matches_no_current_finding_fails_by_default(self):
        findings: dict = {}
        policy = {"GHSA-STALE-STALE-0001": _policy_entry("GHSA-STALE-STALE-0001")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "STALE" in failures[0]

    def test_a_stale_policy_entry_is_permitted_with_the_opt_in_flag(self):
        findings: dict = {}
        policy = {"GHSA-STALE-STALE-0001": _policy_entry("GHSA-STALE-STALE-0001")}
        failures, exempted = evaluate(findings, policy, today=TODAY, allow_stale_policy_entries=True)
        assert failures == []

    def test_a_policy_entry_matching_a_critical_finding_is_not_flagged_as_stale(self):
        # The finding is rejected for being CRITICAL, not because the
        # policy entry is unused -- it very much matches a current finding.
        findings = {"GHSA-AAAA-BBBB-CCCC": {"severity": "critical", "packages": {"metro"}, "urls": set()}}
        policy = {"GHSA-AAAA-BBBB-CCCC": _policy_entry("GHSA-AAAA-BBBB-CCCC")}
        failures, exempted = evaluate(findings, policy, today=TODAY)
        assert len(failures) == 1
        assert "CRITICAL" in failures[0]
        assert not any("STALE" in f for f in failures)


class TestSchemaValidation:
    def test_missing_vulnerabilities_key_is_rejected_as_a_schema_mismatch(self):
        try:
            _validate_schema({"metadata": {"vulnerabilities": {"total": 0}}})
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "vulnerabilities" in str(exc)

    def test_missing_metadata_total_is_rejected_as_a_schema_mismatch(self):
        try:
            _validate_schema({"vulnerabilities": {}, "metadata": {}})
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "metadata" in str(exc)

    def test_a_vuln_entry_missing_via_is_rejected_as_a_schema_mismatch(self):
        report = {
            "vulnerabilities": {"some-pkg": {"severity": "high"}},
            "metadata": {"vulnerabilities": {"total": 1}},
        }
        try:
            _validate_schema(report)
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "via" in str(exc)

    def test_a_well_formed_report_passes_schema_validation(self):
        report = _audit_report(
            [{"package": "a", "severity": "high", "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}]
        )
        _validate_schema(report)  # should not raise


class TestLoadPolicy:
    def test_duplicate_advisory_ids_in_the_policy_file_are_rejected(self, tmp_path):
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "exceptions": [
                        _policy_entry("GHSA-DUPE-DUPE-DUPE"),
                        _policy_entry("GHSA-DUPE-DUPE-DUPE"),
                    ]
                }
            )
        )
        try:
            _load_policy(policy_path)
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "DUPLICATE" in str(exc)

    def test_an_entry_missing_advisory_id_is_rejected(self, tmp_path):
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({"exceptions": [{"package": "foo"}]}))
        try:
            _load_policy(policy_path)
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "advisory_id" in str(exc)

    def test_a_missing_exceptions_list_is_rejected(self, tmp_path):
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({"not_exceptions": []}))
        try:
            _load_policy(policy_path)
            assert False, "expected AuditGateError"
        except AuditGateError as exc:
            assert "exceptions" in str(exc)


class TestMainIntegration:
    """End-to-end tests that exercise `main()` itself (argv parsing, file
    I/O, exit codes) rather than the internal helpers directly."""

    def _minimal_policy(self, tmp_path, advisory_id: str = "GHSA-AAAA-BBBB-CCCC") -> Path:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({"exceptions": [_policy_entry(advisory_id)]}))
        return policy_path

    def test_metadata_reports_vulnerabilities_but_zero_are_extracted_fails_closed(self, tmp_path, capsys):
        from npm_audit_gate import main

        audit_path = tmp_path / "audit.json"
        # metadata says 3 vulnerabilities exist, but the vulnerabilities
        # object itself is empty -- a parsing/schema mismatch, not "clean".
        audit_path.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 3}}}))
        policy_path = self._minimal_policy(tmp_path)

        exit_code = main(["--policy", str(policy_path), "--audit-json", str(audit_path)])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "USAGE ERROR" in captured.err
        assert "ZERO advisory records" in captured.err

    def test_genuinely_zero_vulnerabilities_passes_cleanly(self, tmp_path, capsys):
        from npm_audit_gate import main

        audit_path = tmp_path / "audit.json"
        audit_path.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 0}}}))
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps({"exceptions": []}))

        exit_code = main(["--policy", str(policy_path), "--audit-json", str(audit_path)])
        assert exit_code == 0

    def test_a_stale_policy_entry_fails_the_whole_gate_run_by_default(self, tmp_path, capsys):
        from npm_audit_gate import main

        audit_path = tmp_path / "audit.json"
        audit_path.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 0}}}))
        policy_path = self._minimal_policy(tmp_path)  # entry matches nothing in the (empty) audit report

        exit_code = main(["--policy", str(policy_path), "--audit-json", str(audit_path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "STALE" in captured.err

    def test_a_stale_policy_entry_is_allowed_with_the_cli_flag(self, tmp_path, capsys):
        from npm_audit_gate import main

        audit_path = tmp_path / "audit.json"
        audit_path.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 0}}}))
        policy_path = self._minimal_policy(tmp_path)

        exit_code = main(
            ["--policy", str(policy_path), "--audit-json", str(audit_path), "--allow-stale-policy-entries"]
        )
        assert exit_code == 0

    def test_a_duplicate_advisory_id_in_the_policy_file_fails_as_a_usage_error(self, tmp_path, capsys):
        from npm_audit_gate import main

        audit_path = tmp_path / "audit.json"
        audit_path.write_text(json.dumps({"vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 0}}}))
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "exceptions": [
                        _policy_entry("GHSA-DUPE-DUPE-DUPE"),
                        _policy_entry("GHSA-DUPE-DUPE-DUPE"),
                    ]
                }
            )
        )

        exit_code = main(["--policy", str(policy_path), "--audit-json", str(audit_path)])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "DUPLICATE" in captured.err


class TestRealPolicyFileAgainstTheCurrentRealAuditReport:
    """A regression guard tying this test suite to the ACTUAL policy file
    and the ACTUAL current fire-tv npm audit findings this pass captured
    — not just synthetic fixtures. If a future dependency bump changes
    which advisories fire, this test's own failure is the signal that
    the policy file (or the code) needs a real look, not a silent
    re-approval."""

    def test_the_real_policy_file_parses_and_has_the_expected_entries(self):
        """Local-demo-fixed repair pass: a real `npm install` + `npm audit`
        run in this pass found ZERO current findings for the fire-tv
        project (the three build-time-only Metro/fast-xml-parser
        advisories this test previously pinned - GHSA-gh4j-gqv2-49f6,
        GHSA-w3rx-r6r6-pgpr, GHSA-5p2g-fcmc-qvqq - are no longer present
        in the installed lockfile's resolved versions). Per this test
        class's own stated purpose ("if a future dependency bump changes
        which advisories fire, this test's own failure is the signal...
        not a silent re-approval"), the real policy file was updated to
        drop those now-stale entries (npm_audit_gate.py's own
        STALE-policy-entry check would otherwise flag them), and this
        test now pins the real, current, honest state: no exceptions
        needed. If npm audit ever reports a new finding for this project,
        add a fresh entry to the real policy file (with a real owner,
        exposure classification and justification) and update this
        assertion to match - never re-approve blindly."""
        import json

        policy_path = Path(__file__).resolve().parent.parent.parent / "fire-tv" / "security" / "npm-audit-policy.json"
        raw = json.loads(policy_path.read_text())
        advisory_ids = {e["advisory_id"] for e in raw["exceptions"]}
        assert advisory_ids == set()
        for entry in raw["exceptions"]:
            assert entry["exposure"] == "build_time"
            assert entry.get("owner")
            assert entry.get("reason_for_temporary_acceptance")
            assert entry.get("required_upgrade_path")
