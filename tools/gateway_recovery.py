"""Safe shell-command builders for code-only Edge UI checkpoints and restores."""
from __future__ import annotations

import re


REMOTE_UI_PATH = "/home/swadmin/edge-bacnet-ui-v2"
RECOVERY_ROOT = "/home/swadmin/gw-recovery"
CODE_FILES = ("app.py", "edge_program_engine.py", "README.md", "requirements.txt", "templates")


def release_name(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Edge release must use numeric semantic version form, for example 0.1.8")
    return value


def checkpoint_commands(edge_release: str) -> list[str]:
    release = release_name(edge_release)
    folder = f"{RECOVERY_ROOT}/{release}"
    files = " ".join(CODE_FILES)
    return [
        f"mkdir -p {folder}",
        f"cd {REMOTE_UI_PATH} && tar -czf {folder}/pre-update-code.tar.gz {files}",
        f"sha256sum {folder}/pre-update-code.tar.gz > {folder}/pre-update-code.sha256",
        f"printf '%s\\n' 'scope=code-only; preserves=data/.env/start.sh/site-data' > {folder}/manifest.txt",
        f"ls -lh {folder}/pre-update-code.tar.gz {folder}/pre-update-code.sha256 {folder}/manifest.txt",
    ]


def code_restore_commands(edge_release: str) -> list[str]:
    release = release_name(edge_release)
    folder = f"{RECOVERY_ROOT}/{release}"
    archive = f"{folder}/pre-update-code.tar.gz"
    files = " ".join(CODE_FILES)
    return [
        f"test -s {archive} && cd {folder} && sha256sum -c pre-update-code.sha256",
        "sudo -S -p '' systemctl stop edge-bacnet-ui.service",
        "rm -rf /tmp/edge-ui-code-restore && mkdir -p /tmp/edge-ui-code-restore",
        f"tar -xzf {archive} -C /tmp/edge-ui-code-restore",
        f"test -f /tmp/edge-ui-code-restore/app.py && test -d /tmp/edge-ui-code-restore/templates",
        f"cd /tmp/edge-ui-code-restore && cp -a {files} {REMOTE_UI_PATH}/",
        f"sudo -S -p '' chown -R swadmin:swadmin {REMOTE_UI_PATH}",
        "sudo -S -p '' systemctl start --no-block edge-bacnet-ui.service",
        "sleep 5 && systemctl is-active edge-bacnet-ui.service && curl -I http://127.0.0.1:5000/",
    ]
