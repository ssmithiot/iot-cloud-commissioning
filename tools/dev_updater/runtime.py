"""Local-only runtime guards for the isolated updater."""
from __future__ import annotations

import socket

from . import identity

class PortUnavailable(RuntimeError):
    pass

def require_port(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((identity.DEFAULT_HOST, port))
        probe.listen(1)
    except OSError as error:
        raise PortUnavailable(
            f"{identity.PRODUCT_NAME} cannot start: {identity.DEFAULT_HOST}:{port} is already in use ({error}). "
            f"The Legacy Updater on {identity.LEGACY_PORT} is never stopped or used."
        ) from error
    finally:
        probe.close()
