"""Startup checks and the audit log.

Two jobs that both have to be right before the application is allowed to serve
a page: it must not take a port that belongs to something else, and it must not
write a secret into a file that outlives the session.
"""
from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.dev_updater import identity


class PortUnavailable(RuntimeError):
    """Raised when the configured port cannot be bound."""


@dataclass(frozen=True)
class PortStatus:
    port: int
    host: str
    available: bool
    detail: str


def port_status(port: int, host: str = identity.DEFAULT_HOST) -> PortStatus:
    """Ask the operating system, rather than assuming.

    SO_REUSEADDR is set because the server that follows sets it too
    (ThreadingHTTPServer.allow_reuse_address is 1). The probe has to ask the
    same question the real bind will ask, or it reports a conflict the server
    would not have hit: closing the application leaves the client connections
    in TIME_WAIT for a minute or so, and a probe without SO_REUSEADDR would
    refuse to restart for that whole window.

    It still detects what matters. On Linux SO_REUSEADDR does not permit a
    second listener on the same address and port - that needs SO_REUSEPORT,
    which is deliberately not set - so a genuinely running instance is caught.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        # Bind alone can succeed where listen will not; ask the whole question.
        probe.listen(1)
    except OSError as error:
        return PortStatus(port, host, False, f"{error.strerror or error}")
    finally:
        probe.close()
    return PortStatus(port, host, True, "available")


def require_port(port: int, host: str = identity.DEFAULT_HOST) -> PortStatus:
    """Refuse to start on an occupied port, and say exactly what to do about it."""
    status = port_status(port, host)
    if status.available:
        return status

    hint = ""
    if port == identity.LEGACY_PORT:
        hint = (
            f"\n  Port {identity.LEGACY_PORT} belongs to the Legacy Edge Upgrade Webapp. "
            "The Development Updater must never take it."
        )
    raise PortUnavailable(
        f"{identity.PRODUCT_NAME} cannot start: {host}:{port} is already in use ({status.detail}).{hint}\n"
        f"  Another copy of this application may already be running.\n"
        f"  Choose a different port with --port, or set {identity.PORT_ENV_VAR}.\n"
        f"  The Legacy Updater's port ({identity.LEGACY_PORT}) is never used by this application."
    )


def legacy_port_report(host: str = identity.DEFAULT_HOST) -> str:
    """Report on the Legacy Updater's port without ever binding it.

    Purely informational: whichever way this reads, the Development Updater
    behaves identically. It exists so the operator can see both programs'
    ports side by side.
    """
    status = port_status(identity.LEGACY_PORT, host)
    if status.available:
        return f"Legacy Updater port {identity.LEGACY_PORT}: free (Legacy Updater not currently running)"
    return f"Legacy Updater port {identity.LEGACY_PORT}: in use (Legacy Updater appears to be running - left alone)"


# --- audit log ---------------------------------------------------------------

# Anything matching these is replaced before it can reach a file. The patterns
# cover the shapes a secret arrives in: a labelled field, a sudo prompt echo, a
# GitHub token, an ssh key body, and a URL with credentials inline.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|passphrase)\b(\s*[:=]\s*)(\S+)"), r"\1\2***REDACTED***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "***REDACTED-GITHUB-TOKEN***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "***REDACTED-GITHUB-TOKEN***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "***REDACTED-PRIVATE-KEY***"),
    (re.compile(r"(://[^/\s:@]+):([^/\s@]+)@"), r"\1:***REDACTED***@"),
    (re.compile(r"(?i)(sudo -S -p '')\s*\S+"), r"\1 ***REDACTED***"),
)


def redact(text: str) -> str:
    """Strip anything secret-shaped. Applied to every value on its way to disk."""
    cleaned = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: ("***REDACTED***" if _is_secret_key(key) else redact_value(inner)) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def _is_secret_key(key: object) -> bool:
    return bool(re.search(r"(?i)pass|secret|token|key|credential", str(key)))


class AuditLog:
    """One JSON object per line, in this application's own log directory.

    Never shared with the Legacy Updater, which keeps its log inside its Git
    checkout and has no directory in %ProgramData%.
    """

    def __init__(self, directory: Path | None = None, *, operator: str = "unknown") -> None:
        self.directory = Path(directory) if directory else identity.log_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.operator = operator
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.path = self.directory / f"{identity.APP_NAME}-{stamp}.jsonl"

    def write(self, event: str, **fields: object) -> dict:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "application": identity.APP_NAME,
            "application_version": identity.APP_VERSION,
            "operator": redact(self.operator),
            "event": event,
        }
        for key, value in fields.items():
            record[key] = "***REDACTED***" if _is_secret_key(key) else redact_value(value)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
