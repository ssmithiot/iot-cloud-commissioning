"""Refresh the approved development manifests from GitHub.

The commissioning repository is public, so the manifest and the release artifact
are both retrievable without credentials. This module therefore sends no token
by default, and there is nowhere in the shipped product for one to be baked in:
if a token is ever needed it is read at runtime from an environment variable or
from a file the operator creates by hand, and it never reaches the log.

Refreshing the *list* is a convenience. It cannot widen what may be deployed:
whatever arrives here still goes through release_source.resolve_target(), which
pins to exact commits and verifies the artifact bytes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from tools.dev_updater import identity


REPO = "ssmithiot/iot-cloud-commissioning"
RELEASE_BRANCH = "release/edge-agent-0.2.0"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{RELEASE_BRANCH}"
MANIFEST_DIR = "tools/releases/manifests"
TIMEOUT_SECONDS = 30


class GitHubSourceError(RuntimeError):
    """Raised when the manifest or artifact cannot be retrieved."""


@dataclass(frozen=True)
class TokenSource:
    present: bool
    origin: str


def token_source() -> TokenSource:
    """Where a token would come from, if one were configured.

    Checked in this order and nowhere else. In particular the token is never
    read from the MSI, from installer properties, from the source, or from any
    file this application writes.
    """
    value = os.environ.get(identity.GITHUB_TOKEN_ENV_VAR, "").strip()
    if value:
        return TokenSource(True, f"environment variable {identity.GITHUB_TOKEN_ENV_VAR}")
    token_file = identity.data_dir() / "github-token"
    if token_file.is_file() and token_file.read_text(encoding="utf-8").strip():
        return TokenSource(True, str(token_file))
    return TokenSource(False, "not configured (the repository is public, so none is required)")


def _token() -> str:
    value = os.environ.get(identity.GITHUB_TOKEN_ENV_VAR, "").strip()
    if value:
        return value
    token_file = identity.data_dir() / "github-token"
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def fetch(url: str) -> bytes:
    """Retrieve a URL, failing with a message that says what to do next."""
    request = urllib_request.Request(url, headers={"User-Agent": f"{identity.APP_NAME}/{identity.APP_VERSION}"})
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib_request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib_error.HTTPError as error:
        if error.code in (401, 403, 404):
            configured = token_source()
            raise GitHubSourceError(
                f"GitHub returned HTTP {error.code} for {url}.\n"
                f"  Authentication: {configured.origin}.\n"
                "  If this repository has become private, set "
                f"{identity.GITHUB_TOKEN_ENV_VAR} or create "
                f"{identity.data_dir() / 'github-token'} containing a token with read access, then retry.\n"
                "  The offline copy installed with this application can be used in the meantime."
            ) from error
        raise GitHubSourceError(f"GitHub returned HTTP {error.code} for {url}.") from error
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        raise GitHubSourceError(
            f"Could not reach GitHub for {url}: {error}.\n"
            "  The offline manifest installed with this application can be used instead."
        ) from error


def refresh_manifest(name: str, destination: Path) -> Path:
    """Download one approved manifest beside the installed copies.

    Only manifests for approved development releases are written, so a refresh
    cannot introduce a target this application is not allowed to deploy.
    """
    release = name.removeprefix("edge-").removesuffix(".json")
    if release not in identity.APPROVED_DEV_RELEASES:
        raise GitHubSourceError(
            f"Refusing to fetch manifest '{name}': {release} is not an approved development release."
        )
    payload = fetch(f"{RAW_BASE}/{MANIFEST_DIR}/{name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination
