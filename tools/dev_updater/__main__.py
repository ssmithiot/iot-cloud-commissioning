"""Entry point: hand straight over to the copied updater.

    python -m tools.dev_updater [--port N]

The application is ``updater_webapp``, a copy of the working updater with only
the separation and Edge 0.2.0 changes applied. The port check, configuration
report and startup banner all live in its ``run_server``/``main``, so this
module adds nothing of its own -- a second startup sequence here is exactly the
kind of divergence from the proven tool that the copy exists to avoid.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Installed layout puts this package under <install>/tools/dev_updater; make the
# repository-style "tools.*" imports work from either location.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev_updater.updater_webapp import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
