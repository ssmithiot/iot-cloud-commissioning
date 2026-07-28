from __future__ import annotations

import pytest

from tools.gateway_recovery import checkpoint_commands, checkpoint_inventory_commands, code_restore_commands, release_name


def test_checkpoint_is_code_only_and_preserves_site_data() -> None:
    commands = "\n".join(checkpoint_commands("0.1.8"))
    assert "/home/swadmin/gw-recovery/0.1.8" in commands
    assert "edge_program_engine.py" in commands
    assert "edge_trend_store.py" in commands
    assert "timed_override_store.py" in commands
    assert "router_config.py" in commands
    assert "static" in commands
    assert "-T /home/swadmin/gw-recovery/0.1.8/included.txt" in commands
    assert "preserves=data/.env/start.sh/site-data" in commands


def test_checkpoint_builds_dynamic_existing_inventory_for_legacy_gateway() -> None:
    commands = checkpoint_commands("0.1.9")
    joined = "\n".join(commands)

    assert ": > /home/swadmin/gw-recovery/0.1.9/included.txt" in joined
    assert ": > /home/swadmin/gw-recovery/0.1.9/absent.txt" in joined
    assert 'if [ -e "$item" ]' in joined
    assert 'printf \'%s\\n\' "$item" >> /home/swadmin/gw-recovery/0.1.9/included.txt' in joined
    assert 'printf \'%s\\n\' "$item" >> /home/swadmin/gw-recovery/0.1.9/absent.txt' in joined
    assert "tar -czf /home/swadmin/gw-recovery/0.1.9/pre-update-code.tar.gz -T /home/swadmin/gw-recovery/0.1.9/included.txt" in joined
    assert "tar -czf /home/swadmin/gw-recovery/0.1.9/pre-update-code.tar.gz app.py edge_program_engine.py" not in joined


def test_checkpoint_manifest_records_included_and_absent_items() -> None:
    commands = "\n".join(checkpoint_commands("0.1.9"))

    assert "'included:'" in commands
    assert "sed 's/^/  /' /home/swadmin/gw-recovery/0.1.9/included.txt" in commands
    assert "'absent:'" in commands
    assert "sed 's/^/  /' /home/swadmin/gw-recovery/0.1.9/absent.txt" in commands
    assert "> /home/swadmin/gw-recovery/0.1.9/manifest.txt" in commands


def test_modern_gateway_checkpoint_candidates_include_all_current_files() -> None:
    commands = "\n".join(checkpoint_commands("0.1.9"))

    for item in [
        "app.py",
        "edge_program_engine.py",
        "edge_trend_store.py",
        "timed_override_store.py",
        "router_config.py",
        "README.md",
        "requirements.txt",
        "templates",
        "static",
    ]:
        assert item in commands


def test_restore_requires_checked_code_archive_and_not_full_folder_extract() -> None:
    commands = "\n".join(code_restore_commands("0.1.8"))
    assert "sha256sum -c pre-update-code.sha256" in commands
    assert "tar -xzf /home/swadmin/gw-recovery/0.1.8/pre-update-code.tar.gz -C /tmp/edge-ui-code-restore" in commands
    assert "while IFS= read -r item" in commands
    assert "done < /home/swadmin/gw-recovery/0.1.8/included.txt" in commands


def test_restore_removes_files_absent_before_upgrade() -> None:
    commands = "\n".join(code_restore_commands("0.1.9"))

    assert "done < /home/swadmin/gw-recovery/0.1.9/absent.txt" in commands
    assert "rm -rf -- \"$item\"" in commands
    assert "case \"$item\" in ''|*/*) continue ;; *)" in commands


def test_release_name_rejects_paths() -> None:
    with pytest.raises(ValueError):
        release_name("../../bad")


def test_inventory_lists_only_code_only_checkpoint_archives() -> None:
    command = checkpoint_inventory_commands()[1]
    assert "pre-update-code.tar.gz" in command
    assert "-maxdepth 2" in command
