"""Build and verify an Edge UI artifact from one immutable Git commit."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .commit_resolution import EDGE_UI_REPOSITORY
from .identity import data_dir

REPOSITORY_URL = f"https://github.com/{EDGE_UI_REPOSITORY}.git"
REQUIRED_FILES = ("app.py", "edge_program_engine.py", "edge_trend_store.py", "timed_override_store.py", "router_config.py", "README.md", "requirements.txt")
REQUIRED_DIRS = ("templates", "static")
FORBIDDEN_PARTS = {".git", "tests", "imports", "data", ".local-backups", "__pycache__", ".venv", ".pytest_cache"}
FORBIDDEN_NAMES = {".env", "start.sh"}

class UIArtifactError(RuntimeError):
    pass

@dataclass(frozen=True)
class UIArtifact:
    repository: str
    commit: str
    path: Path
    sha256: str

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def members(source: Path) -> list[Path]:
    selected: list[Path] = []
    for name in REQUIRED_FILES:
        if not (source / name).is_file():
            raise UIArtifactError(f"required UI runtime file is missing: {name}")
        selected.append(Path(name))
    for directory in REQUIRED_DIRS:
        root = source / directory
        if not root.is_dir():
            raise UIArtifactError(f"required UI runtime directory is missing: {directory}")
        selected.extend(path.relative_to(source) for path in root.rglob("*") if path.is_file())
    return sorted(set(selected), key=lambda path: path.as_posix())

def verify_contents(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = [member.name.rstrip("/") for member in archive.getmembers() if member.isfile()]
    expected = set(REQUIRED_FILES)
    if not expected.issubset(names):
        raise UIArtifactError(f"artifact is missing required runtime file(s): {', '.join(sorted(expected - set(names)))}")
    for name in names:
        parts = Path(name).parts
        if name in FORBIDDEN_NAMES or any(part in FORBIDDEN_PARTS for part in parts) or name.endswith((".db", ".sqlite", ".pyc")):
            raise UIArtifactError(f"artifact contains forbidden content: {name}")
        if name not in REQUIRED_FILES and not name.startswith("templates/") and not name.startswith("static/"):
            raise UIArtifactError(f"artifact contains non-runtime content: {name}")

def build_from_checkout(source: Path, commit: str, output: Path) -> UIArtifact:
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip()
    if head != commit or dirty:
        raise UIArtifactError("isolated checkout is not clean at the resolved full commit")
    selected = members(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        import gzip
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for relative in selected:
                    info = archive.gettarinfo(source / relative, arcname=relative.as_posix())
                    info.uid = info.gid = 0; info.uname = info.gname = ""; info.mtime = 0
                    with (source / relative).open("rb") as handle: archive.addfile(info, handle)
    verify_contents(output)
    return UIArtifact(EDGE_UI_REPOSITORY, commit, output, sha256(output))

def cache_root() -> Path:
    return data_dir() / "artifacts" / "edge-ui"

def _cached(commit: str, root: Path) -> UIArtifact | None:
    artifact = root / f"edge-ui-{commit}.tar.gz"
    metadata = artifact.with_suffix(".json")
    if not artifact.is_file() or not metadata.is_file(): return None
    try:
        record = json.loads(metadata.read_text(encoding="utf-8"))
        if record != {"repository": EDGE_UI_REPOSITORY, "commit": commit, "sha256": sha256(artifact)}: return None
        verify_contents(artifact)
    except (OSError, ValueError, UIArtifactError): return None
    return UIArtifact(EDGE_UI_REPOSITORY, commit, artifact, record["sha256"])

def materialize(commit: str, *, root: Path | None = None) -> UIArtifact:
    """Use a verified full-SHA cache or clone a fresh detached checkout."""
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise UIArtifactError("a resolved full 40-character lowercase UI SHA is required")
    root = root or cache_root(); root.mkdir(parents=True, exist_ok=True)
    if cached := _cached(commit, root): return cached
    with tempfile.TemporaryDirectory(prefix="iot-edge-ui-") as temporary:
        checkout = Path(temporary) / "source"
        try:
            subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", REPOSITORY_URL, str(checkout)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", commit], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise UIArtifactError(f"could not materialize UI commit {commit}: {exc}") from exc
        artifact = root / f"edge-ui-{commit}.tar.gz"
        built = build_from_checkout(checkout, commit, artifact)
    built.path.with_suffix(".json").write_text(json.dumps({"repository": built.repository, "commit": built.commit, "sha256": built.sha256}, sort_keys=True), encoding="utf-8")
    return built
