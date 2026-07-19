from __future__ import annotations

from pathlib import Path

from tools.release_checks import validate_release_ledger


def test_current_release_ledger_is_complete() -> None:
    root = Path(__file__).resolve().parents[2]
    assert validate_release_ledger(root) == []
