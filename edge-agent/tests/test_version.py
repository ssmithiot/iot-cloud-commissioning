import re
import tomllib
from pathlib import Path

from iot_cx_agent import __version__


def test_edge_app_version_is_semantic_and_matches_package_metadata() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert pyproject["project"]["version"] == __version__
