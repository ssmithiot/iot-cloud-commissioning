from __future__ import annotations

import pytest

from tools.gateway_recovery import checkpoint_commands, code_restore_commands, release_name


def test_checkpoint_is_code_only_and_preserves_site_data() -> None:
    commands = "\n".join(checkpoint_commands("0.1.8"))
    assert "/home/swadmin/gw-recovery/0.1.8" in commands
    assert "edge_program_engine.py" in commands
    assert "data/" not in commands.split("tar -czf", 1)[1].split("\n", 1)[0]
    assert "preserves=data/.env/start.sh/site-data" in commands


def test_restore_requires_checked_code_archive_and_not_full_folder_extract() -> None:
    commands = "\n".join(code_restore_commands("0.1.8"))
    assert "sha256sum -c pre-update-code.sha256" in commands
    assert "tar -xzf /home/swadmin/gw-recovery/0.1.8/pre-update-code.tar.gz -C /tmp/edge-ui-code-restore" in commands
    assert "cp -a app.py edge_program_engine.py README.md requirements.txt templates /home/swadmin/edge-bacnet-ui-v2/" in commands


def test_release_name_rejects_paths() -> None:
    with pytest.raises(ValueError):
        release_name("../../bad")
