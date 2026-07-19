"""Fail a release build when it is not based on the declared production base."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.release_manifest import load_manifest


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-tag", required=True, help="Exact production tag or commit declared for this build")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    root = args.repository_root.resolve()
    if git(root, "status", "--porcelain"):
        raise SystemExit("Release preflight failed: tracked source has uncommitted changes")
    head = git(root, "rev-parse", "HEAD")
    base = git(root, "rev-parse", args.base_tag)
    subprocess.check_call(["git", "-C", str(root), "merge-base", "--is-ancestor", base, head])
    print(f"Preflight passed: Edge {manifest.edge_release}, base {args.base_tag} ({base[:7]}), HEAD {head[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
