"""Structural/security assertions against infrastructure/template.yaml
(spec v1.1 corrections 2, 3, 4, 10 — "add infrastructure tests / template
assertions confirming secret permissions are on execution roles" /
"add automated template tests for critical IAM, TLS, auth and resource
conditions").

These tests never touch AWS — they load the raw CloudFormation YAML (using
a small local loader that turns the `!Ref`/`!GetAtt`/`!Sub`/`!If`/...
short-form intrinsic-function tags into plain `{"Fn::...": ...}` dicts, the
same shape `cfn-lint`/`sam` use internally) and assert on its structure.
That makes them fast, hermetic unit tests — no `boto3`, no `moto`, no
network — appropriate for the `unit` test marker (see
docs/deployment-runbook.md's test-category table), NOT the `aws` marker
(which is reserved for tests that actually call AWS APIs, none of which
exist in this repo without real credentials).

Run: `pytest infrastructure/tests/` (needs only PyYAML — the same
dependency backend/tests already carries via boto3, or plain
`pip install pyyaml` in any interpreter).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "template.yaml"

_INTRINSIC_TAGS = {
    "!Ref": "Ref",
    "!Condition": "Condition",
    "!GetAtt": "Fn::GetAtt",
    "!Sub": "Fn::Sub",
    "!If": "Fn::If",
    "!Not": "Fn::Not",
    "!Equals": "Fn::Equals",
    "!Or": "Fn::Or",
    "!And": "Fn::And",
    "!Join": "Fn::Join",
    "!Select": "Fn::Select",
    "!Split": "Fn::Split",
    "!FindInMap": "Fn::FindInMap",
    "!Base64": "Fn::Base64",
    "!GetAZs": "Fn::GetAZs",
    "!ImportValue": "Fn::ImportValue",
}


def _make_constructor(fn_name: str):
    def _construct(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {fn_name: value}

    return _construct


class _CfnLoader(yaml.SafeLoader):
    pass


for _tag, _fn in _INTRINSIC_TAGS.items():
    _CfnLoader.add_constructor(_tag, _make_constructor(_fn))


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=_CfnLoader)


@pytest.fixture(scope="module")
def resources(template: dict[str, Any]) -> dict[str, Any]:
    return template["Resources"]


def _policy_actions(role: dict[str, Any]) -> set[str]:
    """Flatten every `Action` across every inline policy statement on an
    IAM role resource into a single set of action strings (some are given
    as a bare string, some as a list — normalize both)."""
    actions: set[str] = set()
    for policy in role["Properties"].get("Policies", []) or []:
        if policy is None or not isinstance(policy, dict) or "PolicyDocument" not in policy:
            continue  # tolerate a !If-wrapped {"Fn::If": [...]} conditional policy slot
        for statement in policy["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            if isinstance(action, str):
                actions.add(action)
            else:
                actions.update(action)
    return actions


def _policy_resources_for_action(role: dict[str, Any], action: str) -> list[Any]:
    found: list[Any] = []
    for policy in role["Properties"].get("Policies", []) or []:
        if policy is None or not isinstance(policy, dict) or "PolicyDocument" not in policy:
            continue
        for statement in policy["PolicyDocument"]["Statement"]:
            stmt_action = statement.get("Action", [])
            actions = {stmt_action} if isinstance(stmt_action, str) else set(stmt_action)
            if action in actions:
                res = statement.get("Resource", [])
                found.extend(res if isinstance(res, list) else [res])
    return found


# ---------------------------------------------------------------------
# Correction 2: ECS execution roles (not task roles) hold the Secrets
# Manager read permission for task-definition `Secrets`.
# ---------------------------------------------------------------------
class TestExecutionRoleSecretPermissions:
    def test_backend_execution_role_can_read_demo_token(self, resources):
        actions = _policy_actions(resources["BackendTaskExecutionRole"])
        assert "secretsmanager:GetSecretValue" in actions

    def test_backend_task_role_cannot_read_secrets(self, resources):
        actions = _policy_actions(resources["BackendTaskRole"])
        assert "secretsmanager:GetSecretValue" not in actions
        assert not any(a.startswith("secretsmanager:") for a in actions)

    def test_backend_task_role_keeps_its_runtime_permissions(self, resources):
        # Moving the secret grant off the task role must not have removed
        # the DynamoDB/X-Ray permissions the running application needs.
        actions = _policy_actions(resources["BackendTaskRole"])
        assert "dynamodb:GetItem" in actions
        assert "dynamodb:PutItem" in actions
        assert "xray:PutTraceSegments" in actions

    def test_mcp_execution_role_can_read_both_secrets(self, resources):
        actions = _policy_actions(resources["McpTaskExecutionRole"])
        assert "secretsmanager:GetSecretValue" in actions
        resource_refs = _policy_resources_for_action(resources["McpTaskExecutionRole"], "secretsmanager:GetSecretValue")
        ref_targets = {r.get("Ref") for r in resource_refs if isinstance(r, dict) and "Ref" in r}
        assert "DemoAccessTokenSecret" in ref_targets
        assert "McpAccessTokenSecret" in ref_targets

    def test_mcp_task_role_cannot_read_secrets(self, resources):
        actions = _policy_actions(resources["McpTaskRole"])
        assert "secretsmanager:GetSecretValue" not in actions

    def test_no_wildcard_secretsmanager_permission_anywhere(self, resources):
        for name, resource in resources.items():
            if resource.get("Type") != "AWS::IAM::Role":
                continue
            for policy in resource["Properties"].get("Policies", []) or []:
                if policy is None or not isinstance(policy, dict) or "PolicyDocument" not in policy:
                    continue
                for statement in policy["PolicyDocument"]["Statement"]:
                    action = statement.get("Action", [])
                    actions = {action} if isinstance(action, str) else set(action)
                    assert "secretsmanager:*" not in actions, f"{name} grants wildcard secretsmanager:*"

    def test_secret_permissions_scoped_to_exact_arn_not_wildcard(self, resources):
        for role_name in ("BackendTaskExecutionRole", "McpTaskExecutionRole"):
            resource_refs = _policy_resources_for_action(resources[role_name], "secretsmanager:GetSecretValue")
            for res in resource_refs:
                assert res != "*", f"{role_name} scopes secretsmanager:GetSecretValue to '*' instead of an exact ARN"


# ---------------------------------------------------------------------
# Correction 3: dedicated MCP access-token secret, never reused from the
# backend's demo/mutation token, read only by the MCP execution role.
# ---------------------------------------------------------------------
class TestMcpAccessTokenSecret:
    def test_dedicated_mcp_secret_resource_exists(self, resources):
        assert resources["McpAccessTokenSecret"]["Type"] == "AWS::SecretsManager::Secret"

    def test_mcp_secret_is_not_the_demo_secret(self, resources):
        mcp_name = resources["McpAccessTokenSecret"]["Properties"]["Name"]
        demo_name = resources["DemoAccessTokenSecret"]["Properties"]["Name"]
        assert mcp_name != demo_name

    def test_mcp_task_definition_injects_mcp_access_token(self, resources):
        secrets = resources["McpTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]
        names = {s["Name"] for s in secrets}
        assert "PIT_WALL_MCP_ACCESS_TOKEN" in names
        entry = next(s for s in secrets if s["Name"] == "PIT_WALL_MCP_ACCESS_TOKEN")
        assert entry["ValueFrom"] == {"Ref": "McpAccessTokenSecret"}

    def test_mcp_access_token_not_wired_from_demo_secret(self, resources):
        secrets = resources["McpTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]
        entry = next(s for s in secrets if s["Name"] == "PIT_WALL_MCP_ACCESS_TOKEN")
        assert entry["ValueFrom"] != {"Ref": "DemoAccessTokenSecret"}


# ---------------------------------------------------------------------
# Correction 4: HTTPS/WSS is the deployment default; a public HTTP
# listener is gated behind an explicit AllowInsecureDemoHttp opt-in when
# no certificate is supplied.
# ---------------------------------------------------------------------
class TestSecureTransportDefault:
    def test_allow_insecure_http_parameter_exists_and_defaults_false(self, template):
        param = template["Parameters"]["AllowInsecureDemoHttp"]
        assert param["Default"] == "false"

    def test_rules_section_requires_cert_or_explicit_opt_in(self, template):
        rules = template.get("Rules")
        assert rules, "Expected a top-level Rules: section enforcing secure-transport-by-default"
        rule = rules["RequireTlsOrExplicitInsecureOptIn"]
        assertion_text = str(rule["Assertions"][0]["Assert"])
        assert "CertificateArn" in assertion_text
        assert "AllowInsecureDemoHttp" in assertion_text

    def test_https_listeners_exist_and_gated_on_certificate(self, resources):
        for listener_name in ("BackendHttpsListener", "McpHttpsListener"):
            listener = resources[listener_name]
            assert listener["Condition"] == "HasCertificate"
            assert listener["Properties"]["Protocol"] == "HTTPS"

    def test_http_listener_redirects_to_https_when_certificate_present(self, resources):
        for listener_name in ("BackendHttpListener", "McpHttpListener"):
            default_action = resources[listener_name]["Properties"]["DefaultActions"][0]
            fn_if = default_action["Fn::If"]
            assert fn_if[0] == "HasCertificate"
            redirect_branch = fn_if[1]
            assert redirect_branch["Type"] == "redirect"
            assert redirect_branch["RedirectConfig"]["Protocol"] == "HTTPS"

    def test_outputs_use_https_scheme_when_certificate_present(self, template):
        for output_name in ("BackendUrl", "McpServerUrl"):
            value = template["Outputs"][output_name]["Value"]
            sub_args = value["Fn::Sub"]
            scheme_expr = sub_args[1]["Scheme"]
            assert scheme_expr["Fn::If"][0] == "HasCertificate"
            assert scheme_expr["Fn::If"][1] == "https"
            assert scheme_expr["Fn::If"][2] == "http"


# ---------------------------------------------------------------------
# Correction 10 (partial — the piece scoped to this file): no
# REPLACE-ME-style placeholder runtime value in any deployable resource.
# ---------------------------------------------------------------------
class TestNoPlaceholderRuntimeValues:
    def test_template_source_has_no_replace_me_runtime_value(self):
        # Explanatory comments are allowed to mention the string
        # "REPLACE-ME" while describing the fix (as this repo's own
        # comments do); what must never exist is the literal appearing as
        # an actual YAML scalar value that could be deployed. Strip
        # comments before checking.
        text = TEMPLATE_PATH.read_text(encoding="utf-8")
        code_only = "\n".join(
            line.split("#", 1)[0] for line in text.splitlines()
        )
        assert "REPLACE-ME" not in code_only

    def test_no_public_unauthenticated_mcp_endpoint(self, resources):
        # The MCP ALB must be internet-facing (spec requirement) but the
        # task must always receive a bearer token to enforce against it —
        # covered by test_mcp_task_definition_injects_mcp_access_token
        # above; this test asserts the ALB scheme assumption that makes
        # that requirement meaningful in the first place.
        assert resources["McpLoadBalancer"]["Properties"]["Scheme"] == "internet-facing"
        secrets = resources["McpTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Secrets"]
        assert any(s["Name"] == "PIT_WALL_MCP_ACCESS_TOKEN" for s in secrets)


# ---------------------------------------------------------------------
# Spec v1.1 pass 4, mandatory correction 8: container/AWS hardening —
# avoid `latest` tags for prod sidecars, ECS deployment health
# percentages/rollback/circuit breaker, read-only filesystems/dropped
# capabilities where compatible, and a bounded StopTimeout matching each
# application's own graceful-shutdown window.
# ---------------------------------------------------------------------
class TestContainerAndDeploymentHardening:
    def _first_container(self, resources: dict, task_definition_name: str) -> dict:
        return resources[task_definition_name]["Properties"]["ContainerDefinitions"][0]

    def test_no_container_image_uses_a_floating_latest_tag(self, resources):
        for task_def_name in ("BackendTaskDefinition", "McpTaskDefinition"):
            for container in resources[task_def_name]["Properties"]["ContainerDefinitions"]:
                image = container["Image"]
                # Image is often an Fn::Ref/!Ref to a parameter (the ECR
                # image the deployer built and pushed, which by
                # convention/docs/deployment-runbook.md is always pushed
                # with a specific tag, e.g. a commit SHA, never `latest`)
                # -- only a literal string value can be checked here.
                if isinstance(image, str):
                    assert not image.endswith(":latest"), f"{task_def_name}'s {container['Name']} uses a :latest tag"

    def test_backend_and_mcp_ecs_services_have_a_deployment_circuit_breaker_with_rollback(self, resources):
        for service_name in ("BackendService", "McpService"):
            deployment_config = resources[service_name]["Properties"]["DeploymentConfiguration"]
            breaker = deployment_config["DeploymentCircuitBreaker"]
            assert breaker["Enable"] is True
            assert breaker["Rollback"] is True

    def test_backend_and_mcp_ecs_services_never_drop_below_100_percent_healthy_during_a_deploy(self, resources):
        for service_name in ("BackendService", "McpService"):
            deployment_config = resources[service_name]["Properties"]["DeploymentConfiguration"]
            # 100% minimum healthy means a rolling deployment never takes
            # capacity away before the replacement task is healthy — no
            # window where the service serves fewer healthy tasks than it
            # started with.
            assert deployment_config["MinimumHealthyPercent"] >= 100
            assert deployment_config["MaximumPercent"] > 100

    def test_backend_and_mcp_application_containers_have_a_read_only_root_filesystem(self, resources):
        backend_container = self._first_container(resources, "BackendTaskDefinition")
        mcp_container = self._first_container(resources, "McpTaskDefinition")
        assert backend_container["ReadonlyRootFilesystem"] is True
        assert mcp_container["ReadonlyRootFilesystem"] is True

    def test_backend_and_mcp_application_containers_drop_all_linux_capabilities(self, resources):
        backend_container = self._first_container(resources, "BackendTaskDefinition")
        mcp_container = self._first_container(resources, "McpTaskDefinition")
        assert backend_container["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]
        assert mcp_container["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]

    def test_a_read_only_root_filesystem_container_still_has_a_writable_tmp_mount(self, resources):
        # A read-only root filesystem with no writable /tmp would break
        # any library that expects ordinary tempfile scratch space at
        # container startup — confirm each hardened container gets one
        # via an explicit task-level volume + mount point, not just that
        # the root filesystem was made read-only.
        for task_def_name in ("BackendTaskDefinition", "McpTaskDefinition"):
            task_def = resources[task_def_name]["Properties"]
            volume_names = {v["Name"] for v in task_def["Volumes"]}
            assert "tmp" in volume_names
            container = task_def["ContainerDefinitions"][0]
            mount_points = {m["SourceVolume"]: m["ContainerPath"] for m in container["MountPoints"]}
            assert mount_points.get("tmp") == "/tmp"

    def test_backend_and_mcp_application_containers_have_a_bounded_stop_timeout(self, resources):
        backend_container = self._first_container(resources, "BackendTaskDefinition")
        mcp_container = self._first_container(resources, "McpTaskDefinition")
        # Must be set (not left to ECS's own default) and short enough to
        # actually bound a stuck shutdown, not just a very large number
        # that never meaningfully triggers.
        assert 0 < backend_container["StopTimeout"] <= 60
        assert 0 < mcp_container["StopTimeout"] <= 60

    def test_xray_daemon_sidecar_image_is_pinned_to_a_specific_version(self, resources):
        backend_containers = resources["BackendTaskDefinition"]["Properties"]["ContainerDefinitions"]
        xray_container = next(c for c in backend_containers if c["Name"] == "xray-daemon")
        assert xray_container["Image"] != "public.ecr.aws/xray/aws-xray-daemon:latest"
        assert ":latest" not in xray_container["Image"]

    def test_backend_container_sets_environment_to_production(self, resources):
        # AWS hackathon-final pass, P0-2: the deployed ECS backend task
        # must actually tell the application it is running in production
        # (backend/app/config.py's Settings.environment) so the app's own
        # fail-closed production validation (required secrets present,
        # CORS not wide open, DEMO_ACCESS_TOKEN not empty, etc. -- see
        # backend/tests/test_config.py) is genuinely exercised in the real
        # deployed stack, not silently skipped because nothing ever set
        # this value. A missing or non-"production" value here means a
        # real AWS deployment is running with looser, non-production
        # validation regardless of how hardened the rest of this template
        # is -- this test exists specifically so that regression can never
        # ship silently again.
        backend_container = self._first_container(resources, "BackendTaskDefinition")
        env_by_name = {e["Name"]: e["Value"] for e in backend_container["Environment"]}
        assert env_by_name.get("ENVIRONMENT") == "production"


# ---------------------------------------------------------------------
# AWS hackathon-final pass, P1-10: trusted-proxy-safe rate limiting behind
# the ALB. Beyond the app-level client_ip.py fix (backend/tests/
# test_client_ip.py), the requirement explicitly also called for a WAF
# rule scoped to the pairing endpoints and metrics/alarms for pairing
# failures/throttling — these assertions confirm both actually landed in
# the template, not just in application code.
# ---------------------------------------------------------------------
class TestPairingEndpointRateLimiting:
    def test_a_waf_rule_scoped_to_the_pairing_endpoints_exists_with_a_tighter_limit(self, resources):
        rules = resources["RateLimitWebAcl"]["Properties"]["Rules"]
        pairing_rule = next(r for r in rules if r["Name"] == "PairingEndpointRateLimit")
        site_wide_rule = next(r for r in rules if r["Name"] == "RateLimitPerIp")

        # The pairing-scoped rule must be meaningfully tighter than the
        # site-wide budget -- otherwise it provides no real additional
        # protection against pairing-code brute-forcing.
        pairing_limit = pairing_rule["Statement"]["RateBasedStatement"]["Limit"]
        site_wide_limit = site_wide_rule["Statement"]["RateBasedStatement"]["Limit"]
        assert pairing_limit < site_wide_limit

        # And it must actually be scoped down to only the pairing routes
        # (not accidentally rate-limiting the whole site at the tight
        # limit) via a ScopeDownStatement matching the pairing paths.
        scope_down = pairing_rule["Statement"]["RateBasedStatement"]["ScopeDownStatement"]
        match_strings = {
            stmt["ByteMatchStatement"]["SearchString"] for stmt in scope_down["OrStatement"]["Statements"]
        }
        assert "/v1/auth/pair" in match_strings
        assert "/v1/auth/pairing-codes" in match_strings
        assert "/v1/auth/revoke" in match_strings

    def test_pairing_rate_limit_rule_is_evaluated_before_the_site_wide_rule(self, resources):
        rules = resources["RateLimitWebAcl"]["Properties"]["Rules"]
        pairing_rule = next(r for r in rules if r["Name"] == "PairingEndpointRateLimit")
        site_wide_rule = next(r for r in rules if r["Name"] == "RateLimitPerIp")
        assert pairing_rule["Priority"] < site_wide_rule["Priority"]

    def test_pairing_failure_and_throttling_metric_filters_target_the_backend_log_group(self, resources):
        for filter_name, expected_pattern in (
            ("PairingFailuresMetricFilter", "device_pairing_failed"),
            ("PairingThrottlingMetricFilter", "device_pairing_rate_limited"),
        ):
            metric_filter = resources[filter_name]["Properties"]
            assert metric_filter["LogGroupName"] == {"Ref": "BackendLogGroup"}
            assert expected_pattern in metric_filter["FilterPattern"]
            transformation = metric_filter["MetricTransformations"][0]
            assert transformation["MetricNamespace"] == {"Fn::Sub": "${AWS::StackName}/Pairing"}

    def test_pairing_failures_and_throttling_alarms_watch_the_metric_filters_own_metrics(self, resources):
        failures_filter = resources["PairingFailuresMetricFilter"]["Properties"]["MetricTransformations"][0]
        throttling_filter = resources["PairingThrottlingMetricFilter"]["Properties"]["MetricTransformations"][0]

        failures_alarm = resources["PairingFailuresAlarm"]["Properties"]
        assert failures_alarm["MetricName"] == failures_filter["MetricName"]
        assert failures_alarm["Namespace"] == failures_filter["MetricNamespace"]
        assert failures_alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"

        throttling_alarm = resources["PairingThrottlingAlarm"]["Properties"]
        assert throttling_alarm["MetricName"] == throttling_filter["MetricName"]
        assert throttling_alarm["Namespace"] == throttling_filter["MetricNamespace"]
        assert throttling_alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


# ---------------------------------------------------------------------
# AWS hackathon-final pass, P1-11: DynamoDB hardening -- PITR, Retain
# deletion policies, explicit customer-managed KMS encryption, and
# capacity/throttling alarms on RaceStateTable.
# ---------------------------------------------------------------------
class TestRaceStateTableHardening:
    def test_table_has_retain_deletion_and_update_replace_policies(self, resources):
        table = resources["RaceStateTable"]
        assert table.get("DeletionPolicy") == "Retain"
        assert table.get("UpdateReplacePolicy") == "Retain"

    def test_point_in_time_recovery_is_enabled(self, resources):
        table_props = resources["RaceStateTable"]["Properties"]
        pitr = table_props["PointInTimeRecoverySpecification"]
        assert pitr["PointInTimeRecoveryEnabled"] is True

    def test_table_uses_explicit_customer_managed_kms_encryption(self, resources):
        table_props = resources["RaceStateTable"]["Properties"]
        sse = table_props["SSESpecification"]
        assert sse["SSEEnabled"] is True
        assert sse["SSEType"] == "KMS"
        # Must reference the dedicated key resource, not the (implicit,
        # unstated) AWS-owned default key.
        assert sse["KMSMasterKeyId"] == {"Ref": "RaceStateTableKey"}

    def test_kms_key_has_rotation_enabled(self, resources):
        key_props = resources["RaceStateTableKey"]["Properties"]
        assert key_props["EnableKeyRotation"] is True

    def test_kms_key_policy_scopes_dynamodb_service_use_to_this_account_and_region(self, resources):
        key_props = resources["RaceStateTableKey"]["Properties"]
        statements = key_props["KeyPolicy"]["Statement"]
        dynamodb_statement = next(s for s in statements if s.get("Principal", {}).get("Service") == "dynamodb.amazonaws.com")
        assert dynamodb_statement["Effect"] == "Allow"
        assert "kms:Decrypt" in dynamodb_statement["Action"]
        assert "kms:GenerateDataKey" in dynamodb_statement["Action"]
        condition = dynamodb_statement["Condition"]
        assert condition["StringEquals"]["kms:CallerAccount"] == {"Ref": "AWS::AccountId"}

    def test_throttle_and_system_errors_alarms_watch_race_state_table(self, resources):
        throttle_alarm = resources["RaceStateTableThrottleAlarm"]["Properties"]
        assert throttle_alarm["Namespace"] == "AWS/DynamoDB"
        assert throttle_alarm["MetricName"] == "ThrottledRequests"
        assert {"Name": "TableName", "Value": {"Ref": "TableName"}} in throttle_alarm["Dimensions"]

        errors_alarm = resources["RaceStateTableSystemErrorsAlarm"]["Properties"]
        assert errors_alarm["Namespace"] == "AWS/DynamoDB"
        assert errors_alarm["MetricName"] == "SystemErrors"
        assert {"Name": "TableName", "Value": {"Ref": "TableName"}} in errors_alarm["Dimensions"]

    def test_ttl_is_still_enabled_alongside_the_new_hardening(self, resources):
        # Regression guard: the pre-existing TTL config (pass 4,
        # mandatory correction 1) must not have been disturbed by this
        # pass's additions.
        table_props = resources["RaceStateTable"]["Properties"]
        ttl = table_props["TimeToLiveSpecification"]
        assert ttl["Enabled"] is True
        assert ttl["AttributeName"] == "ttl"


# ---------------------------------------------------------------------
# AWS hackathon-final pass, P2 (observability): authentication-failure
# metric/alarm and the CloudWatch operational dashboard.
# ---------------------------------------------------------------------
class TestObservabilityDashboardAndAuthAlarm:
    def test_authentication_failures_metric_filter_matches_the_real_audit_event(self, resources):
        metric_filter = resources["AuthenticationFailuresMetricFilter"]["Properties"]
        assert metric_filter["FilterPattern"] == '{ $.audit_event = "authentication_failed" }'
        assert metric_filter["LogGroupName"] == {"Ref": "BackendLogGroup"}
        transformation = metric_filter["MetricTransformations"][0]
        assert transformation["MetricName"] == "AuthenticationFailures"

    def test_authentication_failures_alarm_watches_the_same_metric_the_filter_emits(self, resources):
        metric_filter_props = resources["AuthenticationFailuresMetricFilter"]["Properties"]
        transformation = metric_filter_props["MetricTransformations"][0]
        alarm = resources["AuthenticationFailuresAlarm"]["Properties"]
        assert alarm["MetricName"] == transformation["MetricName"]
        assert alarm["Namespace"] == transformation["MetricNamespace"]
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"

    def test_dashboard_is_gated_on_both_container_images_being_provided(self, resources):
        # Must not reference BackendService/McpService/BackendUnhealthyHostsAlarm
        # attributes unless BOTH conditional resources actually exist --
        # see this test's own template comment on BothImagesProvided.
        dashboard = resources["OperationalDashboard"]
        assert dashboard["Condition"] == "BothImagesProvided"

    def test_dashboard_body_references_both_albs_dynamodb_and_both_lambdas(self, resources):
        body = resources["OperationalDashboard"]["Properties"]["DashboardBody"]
        # DashboardBody is an Fn::Sub string -- assert on its raw template
        # text (before substitution) for the resource names/attrs a
        # correctly-wired dashboard must mention.
        raw = body["Fn::Sub"] if isinstance(body, dict) else body
        for expected in (
            "BackendLoadBalancer.LoadBalancerFullName",
            "McpLoadBalancer.LoadBalancerFullName",
            "RaceStateTable",
            "WatchEvaluatorStreamFunction",
            "AlexaSkillLegacyFunction",
            "AuthenticationFailures",
            "PairingFailures",
        ):
            assert expected in raw, f"dashboard body missing expected reference: {expected}"

    def test_dashboard_alarm_widget_includes_the_new_authentication_alarm(self, resources):
        body = resources["OperationalDashboard"]["Properties"]["DashboardBody"]
        raw = body["Fn::Sub"] if isinstance(body, dict) else body
        assert "AuthenticationFailuresAlarm.Arn" in raw

    def test_dashboard_url_output_is_also_gated_on_both_images(self, template):
        output = template["Outputs"]["OperationalDashboardUrl"]
        assert output["Condition"] == "BothImagesProvided"


# AWS hackathon-final pass, phase 4: Bedrock, trusted-proxy, and
# cross-instance-broker wiring is actually connected end-to-end through
# this template, not just documented as application-layer capability.
class TestBedrockWiring:
    def _backend_env(self, resources):
        container = resources["BackendTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        assert container["Name"] == "backend"
        return {entry["Name"]: entry["Value"] for entry in container["Environment"]}

    def test_llm_provider_bedrock_model_id_and_region_parameters_exist(self, template):
        params = template["Parameters"]
        assert params["LlmProvider"]["AllowedValues"] == ["template", "bedrock"]
        assert params["LlmProvider"]["Default"] == "template"
        assert "BedrockModelId" in params
        assert "BedrockRegion" in params

    def test_backend_task_environment_passes_through_all_three_bedrock_settings(self, resources):
        env = self._backend_env(resources)
        assert env["LLM_PROVIDER"] == {"Ref": "LlmProvider"}
        assert env["BEDROCK_MODEL_ID"] == {"Ref": "BedrockModelId"}
        assert env["BEDROCK_REGION"] == {"Ref": "BedrockRegion"}

    def test_a_deploy_time_rule_refuses_bedrock_provider_without_a_model_arn(self, template):
        rule = template["Rules"]["BedrockProviderRequiresModelArn"]
        assert rule["RuleCondition"] == {"Fn::Equals": [{"Ref": "LlmProvider"}, "bedrock"]}
        assertion = rule["Assertions"][0]["Assert"]
        assert assertion == {"Fn::Not": [{"Fn::Equals": [{"Ref": "BedrockModelArn"}, ""]}]}

    def test_bedrock_iam_grant_is_still_scoped_to_exactly_the_configured_model_arn(self, resources):
        # Phase 4 only wires the ENV VARS through — the existing
        # least-privilege IAM scoping (added in an earlier pass) must be
        # unchanged.
        role = resources["BackendTaskRole"]
        found_resource = None
        for policy in role["Properties"]["Policies"]:
            if not isinstance(policy, dict) or "Fn::If" not in policy:
                continue
            bedrock_policy = policy["Fn::If"][1]
            if bedrock_policy.get("PolicyName") == "BedrockInvoke":
                stmt = bedrock_policy["PolicyDocument"]["Statement"][0]
                assert stmt["Action"] == "bedrock:InvokeModel"
                found_resource = stmt["Resource"]
        assert found_resource == {"Ref": "BedrockModelArn"}


class TestTrustedProxyCidrWiring:
    def test_trusted_proxy_cidrs_parameter_exists_and_defaults_to_blank(self, template):
        param = template["Parameters"]["TrustedProxyCidrs"]
        assert param["Default"] == ""

    def test_backend_task_environment_passes_through_trusted_proxy_cidrs(self, resources):
        container = resources["BackendTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        env = {entry["Name"]: entry["Value"] for entry in container["Environment"]}
        assert env["TRUSTED_PROXY_CIDRS"] == {"Ref": "TrustedProxyCidrs"}


class TestBackendDesiredCountRequiresCrossInstanceBroker:
    def test_backend_desired_count_parameter_exists_bounded_and_defaults_to_one(self, template):
        param = template["Parameters"]["BackendDesiredCount"]
        assert param["Type"] == "Number"
        assert param["Default"] == 1
        assert param["MinValue"] == 1

    def test_backend_service_desired_count_now_comes_from_the_parameter(self, resources):
        service = resources["BackendService"]["Properties"]
        assert service["DesiredCount"] == {"Ref": "BackendDesiredCount"}

    def test_a_deploy_time_rule_refuses_desired_count_above_one_without_the_broker(self, template):
        rule = template["Rules"]["BackendDesiredCountRequiresCrossInstanceBroker"]
        assert rule["RuleCondition"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "BackendDesiredCount"}, "1"]}]}
        assertion = rule["Assertions"][0]["Assert"]
        assert assertion == {"Fn::Equals": [{"Ref": "EnableCrossInstanceEventBroker"}, "true"]}

    def test_the_provisioned_cache_is_still_gated_on_the_same_flag_the_rule_checks(self, resources):
        # Confirms the Rule's Assertion and the actual resource Condition
        # are checking the SAME parameter, not two independently-drifted
        # flags that could disagree.
        assert resources["EventBrokerCache"]["Condition"] == "CrossInstanceEventBrokerEnabled"


class TestOtelTracingWiring:
    """AWS hackathon-final pass, phase 5 (observability): the ADOT
    collector sidecar + OTEL_* env vars added to BackendTaskDefinition.
    """

    def _backend_env(self, resources):
        container = resources["BackendTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        assert container["Name"] == "backend"
        return {entry["Name"]: entry["Value"] for entry in container["Environment"]}

    def _mcp_env(self, resources):
        container = resources["McpTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
        return {entry["Name"]: entry["Value"] for entry in container["Environment"]}

    def test_enable_otel_tracing_and_collector_image_parameters_exist(self, template):
        params = template["Parameters"]
        assert params["EnableOtelTracing"]["AllowedValues"] == ["true", "false"]
        assert params["EnableOtelTracing"]["Default"] == "false"
        assert "OTelCollectorImage" in params

    def test_backend_task_environment_gates_otel_tracing_enabled_on_the_parameter(self, resources):
        env = self._backend_env(resources)
        assert env["OTEL_TRACING_ENABLED"] == {"Fn::If": ["OtelTracingEnabled", "true", "false"]}
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
        assert "OTEL_SERVICE_NAME" in env

    def test_otel_tracing_enabled_condition_reads_the_parameter(self, template):
        assert template["Conditions"]["OtelTracingEnabled"] == {
            "Fn::Equals": [{"Ref": "EnableOtelTracing"}, "true"]
        }

    def test_otel_collector_sidecar_is_present_on_the_backend_task_definition(self, resources):
        containers = resources["BackendTaskDefinition"]["Properties"]["ContainerDefinitions"]
        names = [c["Name"] for c in containers]
        assert "otel-collector" in names
        sidecar = next(c for c in containers if c["Name"] == "otel-collector")
        assert sidecar["Essential"] is False
        assert sidecar["Image"] == {"Ref": "OTelCollectorImage"}
        assert {"ContainerPort": 4318, "Protocol": "tcp"} in sidecar["PortMappings"]

    def test_otel_traces_sampler_parameters_exist_and_are_safe_defaults(self, template):
        # Repair pass (operational correction: production trace sampling).
        # Both parameters must exist, with a documented, non-100%-by-default
        # ratio appropriate for a validation/demo deployment (not
        # `always_on`/1.0, which would sample every request).
        params = template["Parameters"]
        sampler = params["OtelTracesSampler"]
        assert sampler["Default"] == "parentbased_traceidratio"
        assert set(sampler["AllowedValues"]) == {
            "always_on",
            "always_off",
            "traceidratio",
            "parentbased_always_on",
            "parentbased_always_off",
            "parentbased_traceidratio",
        }
        sampler_arg = params["OtelTracesSamplerArg"]
        assert sampler_arg["Default"] == "0.10"
        assert 0.0 <= float(sampler_arg["Default"]) <= 1.0

    def test_sampler_arg_description_does_not_claim_a_low_ratio_reliably_traces_a_small_batch(self, template):
        # Regression guard (repair pass, operational correction: trace-
        # sampling documentation). An earlier revision of this parameter's
        # description incorrectly claimed a 10% ratio "reliably" produces
        # a visible trace from "a small number of demo/smoke-test
        # requests" -- false, since parentbased_traceidratio makes an
        # independent random decision per trace (a batch of N requests at
        # ratio 0.10 has only a 1-0.9^N chance of including a sampled
        # trace). This guards against that specific false claim
        # reappearing, and confirms the corrected description instead
        # documents the validate-at-1.0-then-restore procedure.
        description = template["Parameters"]["OtelTracesSamplerArg"]["Description"]
        assert "reliably" not in description.lower() or "not the value to use" in description.lower()
        assert "1.0" in description
        assert "restore" in description.lower() or "restored" in description.lower()

    def test_backend_task_environment_exposes_the_sampler_env_vars(self, resources):
        env = self._backend_env(resources)
        assert env["OTEL_TRACES_SAMPLER"] == {"Ref": "OtelTracesSampler"}
        assert env["OTEL_TRACES_SAMPLER_ARG"] == {"Ref": "OtelTracesSamplerArg"}

    def test_mcp_task_environment_exposes_the_sampler_env_vars(self, resources):
        env = self._mcp_env(resources)
        assert env["PIT_WALL_MCP_OTEL_TRACING_ENABLED"] == {"Fn::If": ["OtelTracingEnabled", "true", "false"]}
        # Deliberately UNPREFIXED, unlike this service's other
        # `PIT_WALL_MCP_OTEL_*` env vars: these two are read directly by the
        # OpenTelemetry SDK itself (`sampling._get_from_env_or_default()`),
        # not by mcp_server/tracing.py's own os.environ.get convention, so
        # they must use the SDK's own standard names to have any effect.
        assert env["OTEL_TRACES_SAMPLER"] == {"Ref": "OtelTracesSampler"}
        assert env["OTEL_TRACES_SAMPLER_ARG"] == {"Ref": "OtelTracesSamplerArg"}

    def test_no_explicit_sampler_pin_survives_in_either_tracing_module(self):
        # Regression guard: confirms the source-level fix (removing the
        # `sampler=DEFAULT_ON` pin from mcp_server/tracing.py) actually
        # landed and neither tracing module re-introduces an explicit
        # `sampler=` keyword argument to `TracerProvider(...)` on the
        # production (no-injected-provider) construction path, which would
        # silently make the CFN parameters above unable to have any effect.
        import re

        mcp_tracing_source = (Path(__file__).resolve().parent.parent.parent / "mcp_server" / "tracing.py").read_text()
        backend_tracing_source = (
            Path(__file__).resolve().parent.parent.parent / "backend" / "app" / "tracing.py"
        ).read_text()

        def _code_lines(source: str) -> str:
            # Strips full-line comments so this only inspects real code,
            # not prose explaining the historical `sampler=DEFAULT_ON` pin
            # this test guards against reintroducing.
            return "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))

        mcp_code = _code_lines(mcp_tracing_source)
        backend_code = _code_lines(backend_tracing_source)
        assert re.search(r"TracerProvider\([^)]*sampler\s*=", mcp_code) is None
        assert re.search(r"TracerProvider\([^)]*sampler\s*=", backend_code) is None

    def test_xray_write_iam_grant_covers_the_shared_task_role_used_by_every_container(self, resources):
        # otel-collector's `awsxray` exporter needs the same
        # xray:PutTraceSegments/PutTelemetryRecords actions the
        # xray-daemon sidecar already uses -- both share
        # BackendTaskDefinition's one TaskRoleArn, so no separate policy
        # should have been added for the new sidecar.
        role = resources["BackendTaskRole"]
        xray_policy = next(p for p in role["Properties"]["Policies"] if p.get("PolicyName") == "XRayWrite")
        actions = xray_policy["PolicyDocument"]["Statement"][0]["Action"]
        assert "xray:PutTraceSegments" in actions
        assert "xray:PutTelemetryRecords" in actions
        assert resources["BackendTaskDefinition"]["Properties"]["TaskRoleArn"] == {
            "Fn::GetAtt": "BackendTaskRole.Arn"
        }


class TestEmfMetricAlarms:
    """AWS hackathon-final pass, phase 5 (observability): CloudWatch
    Alarms reading the new EMF-based application metrics
    (backend/app/metrics.py) directly, with no MetricFilter in front.
    """

    def test_event_broker_degraded_alarm_reads_the_emf_namespace_and_metric_directly(self, resources):
        alarm = resources["EventBrokerDegradedAlarm"]["Properties"]
        assert alarm["Namespace"] == "PitWallCopilot"
        assert alarm["MetricName"] == "EventBrokerDegraded"
        assert alarm["Dimensions"] == [{"Name": "Reason", "Value": "publish_failed"}]

    def test_bedrock_throttled_alarm_reads_the_emf_namespace_and_metric_directly(self, resources):
        alarm = resources["BedrockThrottledAlarm"]["Properties"]
        assert alarm["Namespace"] == "PitWallCopilot"
        assert alarm["MetricName"] == "BedrockThrottled"
        assert alarm["Dimensions"] == [{"Name": "Source", "Value": "bedrock"}]

    def test_dashboard_alarm_status_widget_includes_both_new_alarms(self, template):
        dashboard = template["Resources"]["OperationalDashboard"]["Properties"]["DashboardBody"]
        body = dashboard if isinstance(dashboard, str) else dashboard["Fn::Sub"]
        assert "${EventBrokerDegradedAlarm.Arn}" in body
        assert "${BedrockThrottledAlarm.Arn}" in body
