"""Explicit GitHub commit resolution; never follows a branch tip."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib import error, parse, request

EDGE_UI_REPOSITORY = "ssmithiot/edge-bacnet-commissioning-ui"
EDGE_AGENT_REPOSITORY = "ssmithiot/iot-cloud-commissioning"
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")

class CommitResolutionError(ValueError):
    pass

@dataclass(frozen=True)
class ResolvedCommit:
    repository: str
    entered_ref: str
    full_sha: str

def resolve_commit(repository: str, entered_ref: str, *, token: str = "", opener=request.urlopen) -> ResolvedCommit:
    """Resolve a full or unambiguous abbreviated object ID through GitHub."""
    ref = entered_ref.strip()
    if not SHA_PATTERN.fullmatch(ref):
        raise CommitResolutionError("Enter a full 40-character SHA or an unambiguous SHA prefix of at least 7 hexadecimal characters.")
    url = f"https://api.github.com/repos/{repository}/commits/{parse.quote(ref, safe='')}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(request.Request(url, headers=headers), timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code == 401:
            raise CommitResolutionError("GitHub authentication unavailable: set GITHUB_TOKEN in the Development Updater .env.") from exc
        if exc.code == 403:
            raise CommitResolutionError(f"GitHub repository inaccessible: token lacks access to {repository}.") from exc
        if exc.code == 404:
            if token:
                raise CommitResolutionError(f"GitHub commit nonexistent in {repository}: {ref}.") from exc
            raise CommitResolutionError(f"GitHub repository inaccessible: set GITHUB_TOKEN to access {repository}.") from exc
        if exc.code == 422 and len(ref) < 40:
            raise CommitResolutionError(f"GitHub short SHA ambiguous or nonexistent in {repository}: {ref}.") from exc
        raise CommitResolutionError(f"GitHub commit nonexistent in {repository}: {ref}.") from exc
    except (error.URLError, TimeoutError, ValueError) as exc:
        raise CommitResolutionError(f"GitHub authentication or network unavailable while resolving {ref} in {repository}.") from exc
    full_sha = str(payload.get("sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", full_sha) or not full_sha.startswith(ref.lower()):
        raise CommitResolutionError(f"Cannot resolve {ref} unambiguously in {repository}.")
    return ResolvedCommit(repository, ref, full_sha)
