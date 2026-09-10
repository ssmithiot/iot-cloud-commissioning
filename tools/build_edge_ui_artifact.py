"""Build an immutable Edge UI code artifact for a release.

The updater deploys this tarball rather than a developer's working copy, so it
must contain exactly the runtime code and none of a gateway's own state. The
archive is written deterministically -- sorted entries, fixed timestamps, no
uid/gid or user names -- so that rebuilding the same source commit reproduces
the same SHA-256 and the manifest checksum stays meaningful.

Usage:
    python tools/build_edge_ui_artifact.py \
        --ui-source /home/steve/projects/iot/edge-bacnet-ui \
        --release 0.2.0
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tarfile


# Kept in step with UI_PACKAGE_FILES / UI_PACKAGE_DIRS / UI_OPTIONAL_PACKAGE_FILES
# in tools/legacy_edge_upgrade_webapp.py, which validates the built artifact.
REQUIRED_FILES = (
    "app.py",
    "edge_program_engine.py",
    "edge_trend_store.py",
    "timed_override_store.py",
    "router_config.py",
    "README.md",
    "requirements.txt",
)
REQUIRED_DIRS = ("templates", "static")
OPTIONAL_FILES = (
    "deploy/iot-cx-edge-router-control.py",
    "deploy/iot-cx-edge-router.sudoers",
    "deploy/install-edge-router-runtime.sh",
    "deploy/router-mstp-nat-advertisement.patch",
    "deploy/edge-bacnet-ui.service.example",
    "deploy/iot-cx-bacnet-router.service.example",
)
# Gateway state and developer clutter must never reach an artifact.
EXCLUDED_PARTS = {"data", ".git", ".local-backups", "__pycache__", ".venv", ".pytest_cache"}
EXCLUDED_SUFFIXES = (".db", ".sqlite", ".pyc")
FIXED_MTIME = 0


def _is_excluded(relative: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return True
    return relative.name.endswith(EXCLUDED_SUFFIXES)


def collect_members(ui_source: Path) -> list[Path]:
    members: list[Path] = []
    for name in REQUIRED_FILES:
        path = ui_source / name
        if not path.is_file():
            raise SystemExit(f"Required UI file is missing: {path}")
        members.append(Path(name))
    for name in OPTIONAL_FILES:
        if (ui_source / name).is_file():
            members.append(Path(name))
    for directory in REQUIRED_DIRS:
        root = ui_source / directory
        if not root.is_dir():
            raise SystemExit(f"Required UI directory is missing: {root}")
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ui_source)
            if _is_excluded(relative):
                continue
            members.append(relative)
    return sorted(set(members), key=lambda item: item.as_posix())


def _reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = FIXED_MTIME
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def build(ui_source: Path, output: Path) -> str:
    members = collect_members(ui_source)
    directories = sorted({parent.as_posix() for member in members for parent in member.parents if parent.as_posix() != "."})
    output.parent.mkdir(parents=True, exist_ok=True)
    # gzip mtime=0 keeps the compressed stream itself reproducible.
    with output.open("wb") as raw:
        import gzip

        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for directory in directories:
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(_reset(info))
                for member in members:
                    archive.add(ui_source / member, arcname=member.as_posix(), filter=_reset)

    digest = hashlib.sha256()
    with output.open("rb") as built:
        for block in iter(lambda: built.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-source", type=Path, required=True, help="Path to the edge-bacnet-ui checkout")
    parser.add_argument("--release", required=True, help="Edge release, e.g. 0.2.0")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    output = args.output or Path(__file__).resolve().parent / "releases" / f"gw006-edge-ui-{args.release}-code.tar.gz"
    sha256 = build(args.ui_source.resolve(), output)
    print(output)
    print(sha256)


if __name__ == "__main__":
    main()
