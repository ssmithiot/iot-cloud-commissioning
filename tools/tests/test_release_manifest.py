from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.release_manifest import load_manifest, sha256_file


def manifest_data() -> dict[str, object]:
    return {
        "edge_release": "0.1.8",
        "base_release": "0.1.7",
        "edge_ui_tag": "edge-ui-v0.1.8",
        "artifact": "tools/releases/edge-ui-0.1.8-code.tar.gz",
        "sha256": "a" * 64,
        "preserves": ["data/", ".env", "start.sh", "gateway identity", "credentials"],
        "rollback_release": "0.1.7",
    }


def test_manifest_requires_protected_gateway_data(tmp_path: Path) -> None:
    data = manifest_data()
    data["preserves"] = ["data/"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="must preserve"):
        load_manifest(path)


def test_manifest_loads_and_hashes_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.tar.gz"
    artifact.write_bytes(b"validated release")
    assert sha256_file(artifact) == "1a1ac94b9abe7f57ea5d404dcdc041b434ffd7dde167bf963684319e0459d058"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_data()), encoding="utf-8")
    assert load_manifest(path).edge_release == "0.1.8"
