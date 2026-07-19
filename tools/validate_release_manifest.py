"""CLI guard used before publishing or deploying an Edge release."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.release_manifest import load_manifest, verify_manifest_artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    print(f"Edge Release {manifest.edge_release}: base={manifest.base_release}, rollback={manifest.rollback_release}")
    if args.metadata_only:
        return 0
    artifact = verify_manifest_artifact(manifest, args.repository_root)
    print(f"Artifact verified: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
