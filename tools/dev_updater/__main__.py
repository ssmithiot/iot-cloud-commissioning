"""Entry point: check the port, then serve.

Refusing to start on an occupied port is the whole point of the startup
sequence. It is checked before the socket is created for real, so the failure
is a sentence the operator can act on rather than a traceback.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Installed layout puts this package under <install>/tools/dev_updater; make the
# repository-style "tools.*" imports work from either location.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev_updater import identity  # noqa: E402
from tools.dev_updater.runtime import AuditLog, PortUnavailable, legacy_port_report, require_port  # noqa: E402
from tools.dev_updater.webapp import make_server  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=identity.APP_NAME, description=identity.PRODUCT_NAME)
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get(identity.PORT_ENV_VAR, identity.DEFAULT_PORT)),
                        help=f"TCP port to serve on (default {identity.DEFAULT_PORT})")
    parser.add_argument("--host", default=identity.DEFAULT_HOST)
    parser.add_argument("--operator", default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if args.port == identity.LEGACY_PORT:
        print(f"Refusing to use port {identity.LEGACY_PORT}: it belongs to the Legacy Edge Upgrade Webapp.",
              file=sys.stderr)
        return 2

    try:
        status = require_port(args.port, args.host)
    except PortUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2

    identity.data_dir().mkdir(parents=True, exist_ok=True)
    audit = AuditLog(operator=args.operator)
    audit.write("startup", port=status.port, host=status.host, legacy_port=identity.LEGACY_PORT)

    server = make_server(args.port, args.host, operator=args.operator)
    url = f"http://{args.host}:{args.port}/"
    print(identity.BANNER)
    print(f"{identity.PRODUCT_NAME} {identity.APP_VERSION}")
    print(f"  serving   {url}")
    print(f"  logs      {identity.log_dir()}")
    print(f"  {legacy_port_report()}")
    print("  Press Ctrl+C to stop.")

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        audit.write("shutdown", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
