"""Safe shell-command builders for code-only Edge UI checkpoints and restores."""
from __future__ import annotations

import re


REMOTE_UI_PATH = "/home/swadmin/edge-bacnet-ui-v2"
RECOVERY_ROOT = "/home/swadmin/gw-recovery"
CODE_FILES = (
    "app.py",
    "edge_program_engine.py",
    "edge_trend_store.py",
    "timed_override_store.py",
    "router_config.py",
    "README.md",
    "requirements.txt",
    "templates",
    "static",
)


def release_name(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Edge release must use numeric semantic version form, for example 0.1.8")
    return value


def checkpoint_commands(edge_release: str) -> list[str]:
    release = release_name(edge_release)
    folder = f"{RECOVERY_ROOT}/{release}"
    candidates = " ".join(CODE_FILES)
    build_lists = (
        f"cd {REMOTE_UI_PATH} && : > {folder}/included.txt && : > {folder}/absent.txt; "
        f"for item in {candidates}; do "
        f"if [ -e \"$item\" ]; then printf '%s\\n' \"$item\" >> {folder}/included.txt; "
        f"else printf '%s\\n' \"$item\" >> {folder}/absent.txt; fi; "
        "done; "
        f"test -s {folder}/included.txt"
    )
    return [
        f"mkdir -p {folder}",
        build_lists,
        f"cd {REMOTE_UI_PATH} && tar -czf {folder}/pre-update-code.tar.gz -T {folder}/included.txt",
        f"sha256sum {folder}/pre-update-code.tar.gz > {folder}/pre-update-code.sha256",
        f"{{ printf '%s\\n' 'scope=code-only' 'preserves=data/.env/start.sh/site-data' 'included:'; sed 's/^/  /' {folder}/included.txt; printf '%s\\n' 'absent:'; sed 's/^/  /' {folder}/absent.txt; }} > {folder}/manifest.txt",
        f"ls -lh {folder}/pre-update-code.tar.gz {folder}/pre-update-code.sha256 {folder}/manifest.txt",
    ]


def checkpoint_inventory_commands() -> list[str]:
    return [
        f"test -d {RECOVERY_ROOT} || mkdir -p {RECOVERY_ROOT}",
        f"find {RECOVERY_ROOT} -mindepth 2 -maxdepth 2 -name 'pre-update-code.tar.gz' -printf '%h %s bytes\\n' | sort || true",
    ]


def code_restore_commands(edge_release: str) -> list[str]:
    release = release_name(edge_release)
    folder = f"{RECOVERY_ROOT}/{release}"
    archive = f"{folder}/pre-update-code.tar.gz"
    return [
        f"test -s {archive} && cd {folder} && sha256sum -c pre-update-code.sha256",
        "sudo -S -p '' systemctl stop edge-bacnet-ui.service",
        "rm -rf /tmp/edge-ui-code-restore && mkdir -p /tmp/edge-ui-code-restore",
        f"tar -xzf {archive} -C /tmp/edge-ui-code-restore",
        f"test -f /tmp/edge-ui-code-restore/app.py && test -d /tmp/edge-ui-code-restore/templates",
        f"cd /tmp/edge-ui-code-restore && while IFS= read -r item; do [ -e \"$item\" ] && cp -a \"$item\" {REMOTE_UI_PATH}/; done < {folder}/included.txt",
        f"cd {REMOTE_UI_PATH} && if [ -f {folder}/absent.txt ]; then while IFS= read -r item; do case \"$item\" in ''|*/*) continue ;; *) rm -rf -- \"$item\" ;; esac; done < {folder}/absent.txt; fi",
        f"sudo -S -p '' chown -R swadmin:swadmin {REMOTE_UI_PATH}",
        "sudo -S -p '' systemctl start --no-block edge-bacnet-ui.service",
        "sleep 5 && systemctl is-active edge-bacnet-ui.service && curl -I http://127.0.0.1:5000/",
    ]
