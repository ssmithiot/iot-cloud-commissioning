"""Regression coverage for the import-template controller selector."""

from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "app" / "ui.py"


def test_render_tree_refreshes_import_template_targets_after_storing_tree() -> None:
    source = UI_PATH.read_text(encoding="utf-8")
    render_tree = source.split("  function renderTree(tree) {", 1)[1].split("\n  function groupOptions()", 1)[0]

    assignment_index = render_tree.index("currentGatewayTree = tree;")
    target_refresh_index = render_tree.index("renderPointTableTemplateTargets();")

    assert assignment_index < target_refresh_index
