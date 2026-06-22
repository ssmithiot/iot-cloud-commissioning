import os
import re
import subprocess
from typing import Any

from iot_cx_agent.config import AgentConfig


BACNET_READ_OBJECT_TYPES = {
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "multi-state-input",
    "multi-state-output",
    "multi-state-value",
}
BACNET_PRESENT_VALUE = "present-value"
BACNET_PRESENT_VALUE_PROPERTY_ID = 85


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


def _required_int(request: dict[str, Any], field_name: str) -> int:
    value = request.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def validate_bacnet_read_request(request: dict[str, Any]) -> dict[str, object]:
    device_instance = _required_int(request, "device_instance")
    object_instance = _required_int(request, "object_instance")

    object_type = request.get("object_type")
    if not isinstance(object_type, str) or object_type not in BACNET_READ_OBJECT_TYPES:
        allowed = ", ".join(sorted(BACNET_READ_OBJECT_TYPES))
        raise ValueError(f"object_type must be one of: {allowed}")

    property_name = request.get("property", BACNET_PRESENT_VALUE)
    if property_name != BACNET_PRESENT_VALUE:
        raise ValueError("property must be present-value")

    return {
        "job_type": "bacnet_read",
        "device_instance": device_instance,
        "object_type": object_type,
        "object_instance": object_instance,
        "property": BACNET_PRESENT_VALUE,
        "property_id": BACNET_PRESENT_VALUE_PROPERTY_ID,
    }


def build_bacnet_read_args(config: AgentConfig, normalized_request: dict[str, object]) -> list[str]:
    return [
        config.bacrp_path,
        str(normalized_request["device_instance"]),
        str(normalized_request["object_type"]),
        str(normalized_request["object_instance"]),
        str(normalized_request["property_id"]),
    ]


def _clean_candidate(raw_value: str) -> str:
    cleaned = raw_value.strip().strip('"')
    for prefix in ("Real:", "Unsigned:", "Enumerated:", "Boolean:", "CharacterString:"):
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned


def _coerce_bacnet_value(raw_value: str) -> object:
    cleaned = _clean_candidate(raw_value)
    lowered = cleaned.lower()
    if lowered in {"active", "inactive"}:
        return lowered
    if lowered in {"true", "false"}:
        return lowered == "true"

    number_match = re.match(r"^([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\b", cleaned)
    if number_match is not None:
        number_text = number_match.group(1)
        if "." in number_text or "e" in number_text.lower():
            return float(number_text)
        return int(number_text)

    return cleaned


def parse_bacnet_read_value(raw_output: str) -> tuple[object | None, str | None]:
    lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    candidates: list[str] = []

    for line in lines:
        lowered = line.lower()
        if "error" in lowered or "timeout" in lowered:
            continue

        for pattern in (
            r"present[-_ ]value\s*(?:\([^)]*\))?\s*[:=]\s*(.+)$",
            r"\bvalue\b\s*[:=]\s*(.+)$",
        ):
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match is not None:
                candidates.append(match.group(1).strip())

    if not candidates and len(lines) == 1:
        candidates.append(lines[0])

    if not candidates:
        return None, None

    raw_value = _clean_candidate(candidates[-1])
    return _coerce_bacnet_value(raw_value), raw_value


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _failure_result(
    base_result: dict[str, object],
    error: str,
    raw_output: str | None = None,
) -> dict[str, object]:
    result = dict(base_result)
    result["status"] = "error"
    result["error"] = error
    if raw_output:
        result["raw_output"] = raw_output
    return result


def run_bacnet_read(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        normalized = validate_bacnet_read_request(request)
    except ValueError as exc:
        result = _failure_result({"job_type": "bacnet_read", "property": BACNET_PRESENT_VALUE, "property_id": 85}, str(exc))
        return result, str(exc)

    env = os.environ.copy()
    env["BACNET_IP_PORT"] = str(config.bacnet_default_port)
    args = build_bacnet_read_args(config, normalized)

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=config.bacnet_timeout_sec,
        )
    except FileNotFoundError:
        error = f"BACnet read command not found: {config.bacrp_path}"
        return _failure_result(normalized, error), error
    except subprocess.TimeoutExpired as exc:
        raw_output = "\n".join(part for part in (_text(exc.stdout).strip(), _text(exc.stderr).strip()) if part)
        error = f"BACnet read command timed out after {config.bacnet_timeout_sec} seconds"
        return _failure_result(normalized, error, raw_output or None), error
    except OSError as exc:
        error = f"BACnet read command failed to start: {exc}"
        return _failure_result(normalized, error), error

    raw_output = completed.stdout
    combined_output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        error = f"BACnet read command failed: {detail}"
        return _failure_result(normalized, error, combined_output or None), error

    value, raw_value = parse_bacnet_read_value(raw_output)
    if raw_value is None:
        error = "BACnet read command output did not contain a readable present-value"
        return _failure_result(normalized, error, combined_output or raw_output), error

    result = dict(normalized)
    result.update(
        {
            "value": value,
            "raw_value": raw_value,
            "status": "ok",
        }
    )
    return result, None
