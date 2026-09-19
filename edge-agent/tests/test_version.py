import re
import tomllib
from pathlib import Path

from iot_cx_agent import __version__
from iot_cx_agent.config import load_config


def test_edge_app_version_is_semantic_and_matches_package_metadata() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert __version__ == "0.2.5"
    assert pyproject["project"]["version"] == __version__


def test_release_example_reports_paired_ui_and_agent_versions() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(example_path)

    assert config.agent_version == "0.2.5"
    assert config.ui_version == "0.2.5"
