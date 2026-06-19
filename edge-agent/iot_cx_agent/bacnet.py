import os
import subprocess
from typing import Any

from iot_cx_agent.config import AgentConfig


def parse_bacwi_output(raw_output: str) -> list[dict[str, object]]:
    devices: list[dict[str, object]] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        parts = line.split()
        if len(parts) < 5:
            continue

        try:
            devices.append(
                {
                    "device_id": int(parts[0]),
                    "mac": parts[1],
                    "network": int(parts[2]),
                    "sadr": parts[3],
                    "apdu": int(parts[4]),
                }
            )
        except ValueError:
            continue

    return devices


def run_bacnet_discovery(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object] | None, str | None]:
    try:
        port = int(request.get("port", config.bacnet_default_port))
        timeout_sec = int(request.get("timeout_sec", config.bacnet_timeout_sec))
    except (TypeError, ValueError):
        return None, "BACnet discovery request has invalid port or timeout_sec"

    env = os.environ.copy()
    env["BACNET_IP_PORT"] = str(port)

    try:
        completed = subprocess.run(
            [config.bacwi_path],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return None, f"BACnet discovery command not found: {config.bacwi_path}"
    except subprocess.TimeoutExpired:
        return None, f"BACnet discovery command timed out after {timeout_sec} seconds"
    except OSError as exc:
        return None, f"BACnet discovery command failed to start: {exc}"

    raw_output = completed.stdout
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        return None, f"BACnet discovery command failed: {detail}"

    devices = parse_bacwi_output(raw_output)
    return (
        {
            "bacnet_discover": True,
            "port": port,
            "devices": devices,
            "raw_output": raw_output,
            "device_count": len(devices),
        },
        None,
    )
