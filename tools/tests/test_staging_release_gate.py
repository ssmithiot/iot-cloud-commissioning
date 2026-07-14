"""Safety-guard and decision-logic tests for the staging release gate.

No network calls: these tests exercise the pure host-refusal validation and
the pass/fail decision functions directly, the same style as
test_release_smoke_check.py and test_staging_load_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.staging_release_gate import (  # noqa: E402
    PRODUCTION_HOST_DENYLIST,
    SYNTHETIC_PREFIX,
    GateChecks,
    GateConfigurationError,
    build_synthetic_gateway,
    exit_code_for,
    load_config,
    preflight_passed,
    validate_staging_url,
)


# --- Host / URL refusal -----------------------------------------------------


def test_refuses_known_production_hosts() -> None:
    for host in PRODUCTION_HOST_DENYLIST:
        with pytest.raises(GateConfigurationError):
            validate_staging_url(f"https://{host}")


def test_refuses_production_host_regardless_of_scheme_or_path() -> None:
    with pytest.raises(GateConfigurationError):
        validate_staging_url("https://iot-cloud-api-dev.onrender.com/health")
    with pytest.raises(GateConfigurationError):
        validate_staging_url("http://iot-cloud-api-dev.onrender.com")


def test_refuses_production_host_case_insensitively() -> None:
    with pytest.raises(GateConfigurationError):
        validate_staging_url("https://IOT-CLOUD-API-DEV.onrender.com")


def test_refuses_subdomain_of_production_host() -> None:
    # Substring match on the hostname catches subdomain tricks like a
    # lookalike front door pointed at the real production host.
    with pytest.raises(GateConfigurationError):
        validate_staging_url("https://evil.iot-cloud-api-dev.onrender.com")


def test_refuses_extra_operator_supplied_fingerprints() -> None:
    with pytest.raises(GateConfigurationError):
        validate_staging_url(
            "https://some-other-host.example.com",
            extra_fingerprints="some-other-host",
        )


def test_extra_fingerprints_are_additive_not_a_replacement() -> None:
    # Supplying extras must not shrink the built-in denylist.
    with pytest.raises(GateConfigurationError):
        validate_staging_url(
            "https://iot-cloud-api-dev.onrender.com",
            extra_fingerprints="totally-unrelated-fragment",
        )


def test_accepts_a_clean_staging_url_and_strips_trailing_slash() -> None:
    assert validate_staging_url("https://iot-cloud-api-staging.onrender.com/") == "https://iot-cloud-api-staging.onrender.com"


def test_rejects_relative_or_schemeless_urls() -> None:
    with pytest.raises(GateConfigurationError):
        validate_staging_url("staging.example.com")
    with pytest.raises(GateConfigurationError):
        validate_staging_url("ftp://staging.example.com")


def test_rejects_empty_url() -> None:
    with pytest.raises(GateConfigurationError):
        validate_staging_url("")


# --- Required environment variables -----------------------------------------


def test_load_config_requires_base_url_and_admin_token() -> None:
    with pytest.raises(GateConfigurationError):
        load_config({})
    with pytest.raises(GateConfigurationError):
        load_config({"STAGING_BASE_URL": "https://iot-cloud-api-staging.onrender.com"})
    with pytest.raises(GateConfigurationError):
        load_config({"STAGING_ADMIN_TOKEN": "token"})


def test_load_config_refuses_production_before_reading_optional_vars() -> None:
    with pytest.raises(GateConfigurationError):
        load_config(
            {
                "STAGING_BASE_URL": "https://iot-cloud-api-dev.onrender.com",
                "STAGING_ADMIN_TOKEN": "token",
            }
        )


def test_load_config_succeeds_with_minimal_valid_env() -> None:
    config = load_config(
        {
            "STAGING_BASE_URL": "https://iot-cloud-api-staging.onrender.com/",
            "STAGING_ADMIN_TOKEN": "token",
        }
    )
    assert config.base_url == "https://iot-cloud-api-staging.onrender.com"
    assert config.admin_token == "token"
    assert config.request_timeout_sec == 30.0
    assert config.skip_cleanup is False
    assert config.report_path is None


def test_load_config_parses_optional_overrides() -> None:
    config = load_config(
        {
            "STAGING_BASE_URL": "https://iot-cloud-api-staging.onrender.com",
            "STAGING_ADMIN_TOKEN": "token",
            "STAGING_GATE_REQUEST_TIMEOUT_SEC": "12.5",
            "STAGING_GATE_SKIP_CLEANUP": "true",
            "STAGING_GATE_REPORT_PATH": "/tmp/report.json",
        }
    )
    assert config.request_timeout_sec == 12.5
    assert config.skip_cleanup is True
    assert config.report_path == "/tmp/report.json"


def test_load_config_rejects_non_numeric_timeout() -> None:
    with pytest.raises(GateConfigurationError):
        load_config(
            {
                "STAGING_BASE_URL": "https://iot-cloud-api-staging.onrender.com",
                "STAGING_ADMIN_TOKEN": "token",
                "STAGING_GATE_REQUEST_TIMEOUT_SEC": "not-a-number",
            }
        )


# --- Smoke-gate decision logic ----------------------------------------------


def test_exit_code_zero_when_all_checks_pass() -> None:
    checks = GateChecks()
    checks.record("a", True)
    checks.record("b", True)
    assert exit_code_for(checks) == 0


def test_exit_code_one_when_any_check_fails() -> None:
    checks = GateChecks()
    checks.record("a", True)
    checks.record("b", False, "boom")
    assert exit_code_for(checks) == 1


def test_exit_code_one_when_no_checks_ran() -> None:
    # An empty result set must never be treated as a silent pass.
    checks = GateChecks()
    assert checks.all_passed is False
    assert exit_code_for(checks) == 1


def test_preflight_passed_requires_every_preflight_check_to_pass() -> None:
    checks = GateChecks()
    checks.record("health_ok", True)
    checks.record("environment_is_staging", True)
    checks.record("database_health", True)
    checks.record("schema_current", True)
    assert preflight_passed(checks) is True

    checks.record("admin_token_authenticates", False, "401")
    assert preflight_passed(checks) is False


def test_preflight_failure_blocks_synthetic_phase_even_with_later_passes() -> None:
    # Simulates: environment check fails, but if code continued to record
    # unrelated passing checks afterward, the gate must still refuse to
    # provision. This is the property that keeps synthetic writes off
    # anything that didn't prove itself staging first.
    checks = GateChecks()
    checks.record("health_ok", True)
    checks.record("environment_is_staging", False, "expected 'staging', got 'production'")
    assert preflight_passed(checks) is False
    # Even if unrelated checks were somehow recorded afterward, one failure
    # keeps the whole preflight (and therefore the whole gate) failed.
    checks.record("database_health", True)
    assert preflight_passed(checks) is False
    assert exit_code_for(checks) == 1


def test_all_passed_is_false_until_something_has_run() -> None:
    assert GateChecks().all_passed is False


# --- Synthetic resource naming ----------------------------------------------


def test_synthetic_gateway_ids_carry_the_smoke_prefix() -> None:
    gateway = build_synthetic_gateway()
    assert gateway.gateway_id.startswith(SYNTHETIC_PREFIX)
    assert gateway.site_id.startswith(SYNTHETIC_PREFIX)
    assert gateway.gateway_token is None


def test_synthetic_gateway_ids_are_unique_per_call() -> None:
    first = build_synthetic_gateway()
    second = build_synthetic_gateway()
    assert first.gateway_id != second.gateway_id
    assert first.site_id != second.site_id
