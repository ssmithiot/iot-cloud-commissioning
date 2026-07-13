"""Safety-guard tests for the release smoke checker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.release_smoke_check import (  # noqa: E402
    FORBIDDEN_SYNTHETIC_HOSTS,
    SYNTHETIC_PREFIX,
    parse_args,
    synthetic_allowed,
    validate_base_url,
)


def test_synthetic_mode_refused_on_production_hosts() -> None:
    for host in FORBIDDEN_SYNTHETIC_HOSTS:
        assert not synthetic_allowed(f"https://{host}")
        with pytest.raises(SystemExit):
            parse_args([
                "--base-url", f"https://{host}",
                "--expect-environment", "staging",
                "--admin-token", "token",
                "--synthetic",
            ])


def test_read_only_mode_allowed_on_production_hosts() -> None:
    args = parse_args([
        "--base-url", "https://iot-cloud-api-dev.onrender.com",
        "--expect-environment", "production",
        "--read-only",
    ])
    assert args.read_only and not args.synthetic


def test_synthetic_requires_admin_token_and_non_production_expectation() -> None:
    with pytest.raises(SystemExit):
        parse_args([
            "--base-url", "https://staging.example.com",
            "--expect-environment", "staging",
            "--synthetic",
        ])
    with pytest.raises(SystemExit):
        parse_args([
            "--base-url", "https://staging.example.com",
            "--expect-environment", "production",
            "--admin-token", "token",
            "--synthetic",
        ])


def test_mode_flag_is_required_and_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--base-url", "https://staging.example.com", "--expect-environment", "staging"])
    with pytest.raises(SystemExit):
        parse_args([
            "--base-url", "https://staging.example.com",
            "--expect-environment", "staging",
            "--read-only", "--synthetic",
        ])


def test_url_validation_and_synthetic_prefix() -> None:
    with pytest.raises(SystemExit):
        validate_base_url("staging.example.com")
    assert validate_base_url("https://staging.example.com/") == "https://staging.example.com"
    assert SYNTHETIC_PREFIX == "SMOKE-"
