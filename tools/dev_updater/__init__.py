"""IOT Edge Development Updater.

A separately installed, manual-only updater for deploying Edge 0.2.0 release
candidates to hand-picked test gateways. It installs and runs side by side with
the Legacy Edge Upgrade Webapp and shares no port, directory, configuration,
log, lock file or credential with it.
"""
from tools.dev_updater.identity import APP_NAME, APP_VERSION, PRODUCT_NAME

__all__ = ["APP_NAME", "APP_VERSION", "PRODUCT_NAME"]
__version__ = APP_VERSION
