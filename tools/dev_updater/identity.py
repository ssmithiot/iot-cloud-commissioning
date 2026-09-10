"""All identity values unique to the Development Updater."""
from __future__ import annotations

import os
from pathlib import Path

PRODUCT_NAME = "IOT Edge Development Updater"
APP_NAME = "IOTEdgeDevUpdater"
APP_VERSION = "0.2.0-dev.5"
# Windows Installer accepts numeric versions only. This maps the displayed
# prerelease version above to the product version used for upgrades.
MSI_PRODUCT_VERSION = "0.2.5"
SOURCE_COMMIT = "4920a96ecc7c5486bc3b323ce16dd4a4766a83ed"
DEFAULT_PORT = 8791
LEGACY_PORT = 8766
DEFAULT_HOST = "127.0.0.1"
UPGRADE_CODE = "AECCDF45-A1D2-43A5-9142-32E6A984A66E"
WINDOWS_INSTALL_DIR = r"C:\Program Files\IOT Edge Development Updater"
WINDOWS_DATA_DIR = r"%ProgramData%\IOT\EdgeDevUpdater"
WINDOWS_LOG_DIR = r"%ProgramData%\IOT\EdgeDevUpdater\logs"
PORT_ENV_VAR = "IOT_EDGE_DEV_UPDATER_PORT"
DATA_DIR_ENV_VAR = "IOT_EDGE_DEV_UPDATER_DATA"

def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override)
    if program_data := os.environ.get("ProgramData"):
        return Path(program_data) / "IOT" / "EdgeDevUpdater"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "iot-edge-dev-updater"

def log_dir() -> Path:
    return data_dir() / "logs"

def env_path() -> Path:
    return data_dir() / ".env"

def pid_path() -> Path:
    return data_dir() / f"{APP_NAME}.pid"
