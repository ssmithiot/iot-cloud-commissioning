"""Safety-guard tests for the staging load harness."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.staging_load_harness import (  # noqa: E402
    FORBIDDEN_HOSTS,
    SYNTHETIC_GATEWAY_PREFIX,
    parse_args,
    validate_base_url,
)


def test_refuses_known_production_hosts() -> None:
    for host in FORBIDDEN_HOSTS:
        with pytest.raises(SystemExit):
            validate_base_url(f"https://{host}")


def test_refuses_relative_and_schemeless_urls() -> None:
    with pytest.raises(SystemExit):
        validate_base_url("staging.example.com")
    with pytest.raises(SystemExit):
        validate_base_url("ftp://staging.example.com")


def test_accepts_staging_url_and_strips_trailing_slash() -> None:
    assert validate_base_url("https://staging.example.com/") == "https://staging.example.com"


def test_requires_confirm_staging_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--base-url", "https://staging.example.com", "--admin-token", "token"])


def test_parse_args_accepts_minimal_staging_invocation() -> None:
    args = parse_args(
        [
            "--base-url",
            "https://staging.example.com",
            "--confirm-staging",
            "--admin-token",
            "token",
        ]
    )
    assert args.base_url == "https://staging.example.com"
    assert args.gateways == 5
    assert SYNTHETIC_GATEWAY_PREFIX == "LOADTEST-"


def test_rejects_excessive_gateway_counts() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--base-url",
                "https://staging.example.com",
                "--confirm-staging",
                "--admin-token",
                "token",
                "--gateways",
                "999999",
            ]
        )
