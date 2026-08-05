"""Identity of the IOT Edge Development Updater.

Everything that could collide with the Legacy Updater Jim runs for Edge 0.1.9
is named here, in one file, so the separation can be read and tested in one
place.  The Legacy Updater's own identity is recorded alongside as constants
this application never writes to and never binds: they exist so the isolation
tests have something concrete to assert against, and so a reviewer can see both
sides of the boundary without opening the other program.

Nothing in this module imports the Legacy Updater.
"""
from __future__ import annotations

import os
from pathlib import Path


# --- the Legacy Updater, recorded but never touched --------------------------

LEGACY_PRODUCT_NAME = "Legacy Edge Upgrade Webapp"
LEGACY_MODULE = "tools/legacy_edge_upgrade_webapp.py"
LEGACY_LAUNCHER = "tools/start-legacy-edge-upgrade-webapp.cmd"
LEGACY_PORT = 8766
LEGACY_VENV_DIR_NAME = ".gateway-update-venv"
LEGACY_EDGE_RELEASE = "0.1.9"


# --- this application --------------------------------------------------------

PRODUCT_NAME = "IOT Edge Development Updater"
APP_NAME = "IOTEdgeDevUpdater"
SHORTCUT_NAME = "IOT Edge Development Updater"
MANUFACTURER = "The Internet of Team, LLC"
APP_VERSION = "0.1.0"

# The only visible difference from the updater this was copied from. Deliberately
# a plain subtitle rather than a warning treatment: the interface should read as
# the same familiar tool so muscle memory carries over, with just enough on the
# page to tell the two apart.
BANNER = "IOT Edge Development Updater"
BANNER_SUBTITLE = "Manual Development Use"

# Stable for the lifetime of the product. Windows recognises an upgrade by this
# code; regenerating it would install a second copy side by side with itself.
# It shares no digits with anything the Legacy Updater uses because the Legacy
# Updater is not an MSI at all - it is a .cmd launcher run from a Git checkout.
UPGRADE_CODE = "90FF1484-46DC-4848-890C-432F735E079D"
DATA_DIR_COMPONENT_GUID = "409F9F49-6E32-4809-985E-60AA275E235C"
SHORTCUT_COMPONENT_GUID = "57F462A5-4939-4DD8-8867-D95E89CB89D9"

WINDOWS_INSTALL_DIR = r"C:\Program Files\IOT Edge Development Updater"
WINDOWS_DATA_DIR = r"%ProgramData%\IOT\EdgeDevUpdater"
WINDOWS_LOG_DIR = r"%ProgramData%\IOT\EdgeDevUpdater\logs"

# Deliberately far from 8766, and from the 8000-8080 range that development web
# servers crowd into. Configurable, and never assumed to be free: see
# port_status() below.
DEFAULT_PORT = 8791
DEFAULT_HOST = "127.0.0.1"

PORT_ENV_VAR = "IOT_EDGE_DEV_UPDATER_PORT"
DATA_DIR_ENV_VAR = "IOT_EDGE_DEV_UPDATER_DATA"
GITHUB_TOKEN_ENV_VAR = "IOT_EDGE_DEV_UPDATER_GITHUB_TOKEN"

# Only these Edge releases may be deployed by the Development Updater. 0.1.9 is
# deliberately absent: production releases belong to the Legacy Updater, and a
# development tool must not be able to reach for one by accident.
APPROVED_DEV_RELEASES = frozenset({"0.2.0"})


def data_dir() -> Path:
    """The Development Updater's own state directory.

    %ProgramData%\\IOT\\EdgeDevUpdater on Windows, an XDG-ish path elsewhere so
    the application and its tests run on the Linux build host too.  The Legacy
    Updater keeps its state inside its Git checkout and has no equivalent
    directory, so these cannot overlap.
    """
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override)
    program_data = os.environ.get("ProgramData")
    if program_data:
        return Path(program_data) / "IOT" / "EdgeDevUpdater"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "iot-edge-dev-updater"


def log_dir() -> Path:
    return data_dir() / "logs"


def config_path() -> Path:
    return data_dir() / "config.json"


def pid_path() -> Path:
    """Named for this product, so the Legacy Updater's process is never seen."""
    return data_dir() / f"{APP_NAME}.pid"


def checkpoint_index_path() -> Path:
    return data_dir() / "checkpoints.jsonl"
