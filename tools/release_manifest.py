"""Validate immutable Edge release manifests before an updater build or rollout.

This module deliberately handles only metadata and hashes; it never handles
credentials or gateway site data.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


REQUIRED_FIELDS = {
    "edge_release",
    "base_release",
    "edge_ui_tag",
    "artifact",
    "sha256",
    "preserves",
    "rollback_release",
}
FORBIDDEN_PRESERVES = {"data/", ".env", "start.sh", "gateway identity", "credentials"}


@dataclass(frozen=True)
class EdgeReleaseManifest:
    edge_release: str
    base_release: str
    edge_ui_tag: str
    artifact: str
    sha256: str
    preserves: tuple[str, ...]
    rollback_release: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> EdgeReleaseManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED_FIELDS - set(raw))
    if missing:
        raise ValueError(f"Release manifest missing required field(s): {', '.join(missing)}")
    if not isinstance(raw["preserves"], list) or not raw["preserves"]:
        raise ValueError("Release manifest preserves must be a non-empty list")
    preserves = tuple(str(value) for value in raw["preserves"])
    missing_preserves = sorted(FORBIDDEN_PRESERVES - set(preserves))
    if missing_preserves:
        raise ValueError(f"Release manifest must preserve: {', '.join(missing_preserves)}")
    sha256 = str(raw["sha256"]).lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("Release manifest sha256 must be 64 lowercase hexadecimal characters")
    return EdgeReleaseManifest(
        edge_release=str(raw["edge_release"]),
        base_release=str(raw["base_release"]),
        edge_ui_tag=str(raw["edge_ui_tag"]),
        artifact=str(raw["artifact"]),
        sha256=sha256,
        preserves=preserves,
        rollback_release=str(raw["rollback_release"]),
    )


def verify_manifest_artifact(manifest: EdgeReleaseManifest, repository_root: Path) -> Path:
    artifact = (repository_root / manifest.artifact).resolve()
    if not artifact.is_file():
        raise ValueError(f"Release artifact does not exist: {artifact}")
    actual = sha256_file(artifact)
    if actual != manifest.sha256:
        raise ValueError(f"Release artifact checksum mismatch: expected {manifest.sha256}, got {actual}")
    return artifact
