"""Resolve a development deployment to exact, immutable commits and hashes.

The Development Updater is allowed to *list* approved manifests from the
repository, and may refresh that list from GitHub.  What it is not allowed to do
is deploy anything that could change underneath it: a branch tip, a moving tag,
origin/main, an abbreviated commit, or an artifact whose bytes no longer match
the hash the release was approved with.

Every deployment therefore passes through resolve_target(), which either returns
a fully pinned target or raises.  There is no path that skips it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from tools.dev_updater.identity import APPROVED_DEV_RELEASES
from tools.release_manifest import EdgeReleaseManifest, load_manifest


FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")

# Names that look like a target but are not one. Checked case-insensitively and
# after stripping a remote prefix, so "origin/MAIN" is caught as readily as
# "main".
MUTABLE_REFS = frozenset({
    "main", "master", "head", "trunk", "develop", "development",
    "latest", "tip", "release", "stable", "current",
})


class ReleaseSourceError(RuntimeError):
    """Raised when a target cannot be pinned to immutable bytes."""


@dataclass(frozen=True)
class PinnedTarget:
    """A deployment target with nothing left to resolve."""

    edge_release: str
    release_candidate: str
    edge_ui_commit: str
    agent_commit: str
    artifact_path: Path
    artifact_name: str
    artifact_sha256: str
    rollback_release: str
    preserves: tuple[str, ...]
    manifest_path: Path
    manifest_sha256: str

    @property
    def summary(self) -> dict[str, str]:
        """What the operator must see before confirming, in display order."""
        return {
            "Release": self.edge_release,
            "Release candidate": self.release_candidate,
            "Target Edge UI commit": self.edge_ui_commit,
            "Target Agent commit": self.agent_commit,
            "Artifact filename": self.artifact_name,
            "Artifact SHA-256": self.artifact_sha256,
            "Rollback release": self.rollback_release,
            "Manifest": self.manifest_path.name,
            "Manifest SHA-256": self.manifest_sha256,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_immutable_ref(value: str, *, field: str) -> str:
    """A target reference must be a full commit and nothing else."""
    candidate = (value or "").strip()
    if not candidate:
        raise ReleaseSourceError(f"{field} is empty; a development deployment must name an exact commit.")
    bare = candidate.split("/")[-1].lower()
    if bare in MUTABLE_REFS or candidate.lower().startswith(("origin/", "refs/heads/")):
        raise ReleaseSourceError(
            f"{field} is '{candidate}', which is a moving reference. "
            "A development deployment must name an exact 40-character commit."
        )
    if not FULL_COMMIT.match(candidate.lower()):
        raise ReleaseSourceError(
            f"{field} is '{candidate}', which is not a 40-character lowercase commit. "
            "Abbreviated commits, tags and branch names are refused because they can move."
        )
    return candidate.lower()


def approved_manifests(manifest_dir: Path) -> list[Path]:
    """Manifests this application is permitted to deploy, newest name last.

    A manifest on disk is not automatically approved. Only the releases in
    APPROVED_DEV_RELEASES qualify, which keeps 0.1.9 - the release the Legacy
    Updater owns - out of reach of this program entirely.
    """
    found: list[Path] = []
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            manifest = load_manifest(path)
        except (ValueError, OSError):
            continue
        if manifest.edge_release in APPROVED_DEV_RELEASES:
            found.append(path)
    return found


def resolve_target(manifest_path: Path, *, repo_root: Path, release_candidate: str = "") -> PinnedTarget:
    """Pin a manifest to exact commits and verified artifact bytes, or raise."""
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise ReleaseSourceError(f"Release manifest not found: {manifest_path}")

    try:
        manifest: EdgeReleaseManifest = load_manifest(manifest_path)
    except ValueError as error:
        raise ReleaseSourceError(f"Release manifest is not valid: {error}") from error

    if manifest.edge_release not in APPROVED_DEV_RELEASES:
        raise ReleaseSourceError(
            f"Edge release {manifest.edge_release} is not an approved development release. "
            f"This application deploys only: {', '.join(sorted(APPROVED_DEV_RELEASES))}. "
            "Production releases are handled by the Legacy Updater."
        )

    edge_ui_commit = assert_immutable_ref(manifest.edge_ui_tag, field="Edge UI commit (edge_ui_tag)")
    agent_commit = assert_immutable_ref(manifest.agent_source_commit, field="Edge Agent commit (agent_source_commit)")

    artifact_path = (repo_root / manifest.artifact).resolve()
    # The artifact path comes from a manifest; it must stay inside the repository.
    if repo_root.resolve() not in artifact_path.parents:
        raise ReleaseSourceError(f"Release artifact resolves outside the repository: {artifact_path}")
    if not artifact_path.is_file():
        raise ReleaseSourceError(
            f"Release artifact is missing: {artifact_path}. "
            "The manifest was approved with this file; deployment cannot proceed without it."
        )

    actual = sha256_file(artifact_path)
    if actual != manifest.sha256:
        raise ReleaseSourceError(
            "Release artifact SHA-256 does not match the approved manifest.\n"
            f"  expected {manifest.sha256}\n"
            f"  actual   {actual}\n"
            "The artifact has changed since the release was approved. Deployment refused."
        )

    return PinnedTarget(
        edge_release=manifest.edge_release,
        release_candidate=release_candidate or f"{manifest.edge_release}-dev",
        edge_ui_commit=edge_ui_commit,
        agent_commit=agent_commit,
        artifact_path=artifact_path,
        artifact_name=artifact_path.name,
        artifact_sha256=actual,
        rollback_release=manifest.rollback_release,
        preserves=manifest.preserves,
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
    )
