"""Release ledger checks executed by the existing Tools CI suite."""
from __future__ import annotations

from pathlib import Path

from tools.release_manifest import load_manifest


EDGE_REQUIRED = ("Status:", "Base release", "## Scope", "## Immutable source", "## Validation and rollback")


def validate_release_ledger(repository_root: Path) -> list[str]:
    errors: list[str] = []
    edge_notes = repository_root / "docs" / "releases" / "edge"
    manifest_dir = repository_root / "tools" / "releases" / "manifests"
    for note in sorted(edge_notes.glob("*.md")):
        text = note.read_text(encoding="utf-8")
        missing = [heading for heading in EDGE_REQUIRED if heading not in text]
        if missing:
            errors.append(f"{note}: missing {', '.join(missing)}")
    for path in sorted(manifest_dir.glob("edge-*.json")):
        manifest = load_manifest(path)
        note = edge_notes / f"{manifest.edge_release}.md"
        if not note.is_file():
            errors.append(f"{path}: missing release note {note}")
            continue
        text = note.read_text(encoding="utf-8")
        if manifest.edge_ui_tag not in text:
            errors.append(f"{path}: release note does not name tag {manifest.edge_ui_tag}")
        if manifest.sha256 not in text:
            errors.append(f"{path}: release note does not record artifact SHA-256")
    return errors
