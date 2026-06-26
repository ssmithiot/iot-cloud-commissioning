import os
import re
import shutil
import subprocess
from pathlib import Path
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
BACNET_LOAD_POINT_OBJECT_TYPES = {
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "calendar",
    "command",
    "event-enrollment",
    "file",
    "loop",
    "multi-state-input",
    "multi-state-output",
    "multi-state-value",
    "notification-class",
    "program",
    "schedule",
    "trend-log",
}
BACNET_OBJECT_TYPE_BY_ID = {
    0: "analog-input",
    1: "analog-output",
    2: "analog-value",
    3: "binary-input",
    4: "binary-output",
    5: "binary-value",
    6: "calendar",
    7: "command",
    8: "device",
    10: "file",
    12: "loop",
    13: "multi-state-input",
    14: "multi-state-output",
    15: "notification-class",
    16: "program",
    17: "schedule",
    19: "multi-state-value",
    20: "trend-log",
}
BACNET_PRESENT_VALUE = "present-value"
BACNET_PRESENT_VALUE_PROPERTY_ID = 85
BACNET_OBJECT_LIST_PROPERTY_ID = 76
BACNET_OBJECT_NAME_PROPERTY_ID = 77
CLOUD_BACNET_PORT = 47814
BACNET_RUNTIME_BUSY = "bacnet_runtime_busy"
BACNET_RUNTIME_BUSY_MESSAGE = "Local commissioning UI is using BACnet port 47814. Cloud BACnet job yielded."


def bacnet_runtime_lock_held(config: AgentConfig) -> bool:
    return config.bacnet_lock_path.exists()


def acquire_bacnet_runtime_lock(config: AgentConfig) -> int | None:
    try:
        config.bacnet_lock_path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(config.bacnet_lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def release_bacnet_runtime_lock(config: AgentConfig, lock_fd: int) -> None:
    os.close(lock_fd)
    try:
        config.bacnet_lock_path.unlink()
    except FileNotFoundError:
        pass


def _command_status(command_path: str) -> dict[str, object]:
    candidate = Path(command_path)
    has_path_part = candidate.is_absolute() or len(candidate.parts) > 1
    resolved_path = candidate if has_path_part else None
    if resolved_path is None:
        found = shutil.which(command_path)
        if found is None:
            return {
                "configured_path": command_path,
                "resolved_path": None,
                "exists": False,
                "executable": False,
            }
        resolved_path = Path(found)
    exists = resolved_path.exists()
    return {
        "configured_path": command_path,
        "resolved_path": str(resolved_path) if exists else None,
        "exists": exists,
        "executable": exists and os.access(resolved_path, os.X_OK),
    }


def _deferred_result(base_result: dict[str, object], config: AgentConfig) -> dict[str, object]:
    result = dict(base_result)
    result.update(
        {
            "status": "deferred",
            "error": BACNET_RUNTIME_BUSY,
            "message": BACNET_RUNTIME_BUSY_MESSAGE,
            "lock_path": str(config.bacnet_lock_path),
            "lock_held": True,
        }
    )
    return result


def run_bacnet_runtime_check(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        port = int(request.get("bacnet_port", request.get("port", config.bacnet_default_port)))
        timeout_sec = int(request.get("timeout_sec", config.bacnet_timeout_sec))
    except (TypeError, ValueError):
        port = config.bacnet_default_port
        timeout_sec = config.bacnet_timeout_sec

    bacwi = _command_status(config.bacwi_path)
    bacrp = _command_status(config.bacrp_path)
    result = {
        "job_type": "bacnet_runtime_check",
        "bacnet_port": port,
        "timeout_sec": timeout_sec,
        "lock_path": str(config.bacnet_lock_path),
        "lock_held": bacnet_runtime_lock_held(config),
        "bacwi_configured_path": bacwi["configured_path"],
        "bacwi_resolved_path": bacwi["resolved_path"],
        "bacwi_exists": bacwi["exists"],
        "bacwi_executable": bacwi["executable"],
        "bacrp_configured_path": bacrp["configured_path"],
        "bacrp_resolved_path": bacrp["resolved_path"],
        "bacrp_exists": bacrp["exists"],
        "bacrp_executable": bacrp["executable"],
    }

    if port != CLOUD_BACNET_PORT:
        error = f"Cloud BACnet jobs must use UDP {CLOUD_BACNET_PORT}"
        result.update({"status": "error", "error": error})
        return result, error
    if timeout_sec <= 0:
        error = "BACnet timeout_sec must be greater than 0"
        result.update({"status": "error", "error": error})
        return result, error
    if not bacwi["executable"]:
        error = f"BACnet discovery command is not executable: {config.bacwi_path}"
        result.update({"status": "error", "error": error})
        return result, error
    if not bacrp["executable"]:
        error = f"BACnet read command is not executable: {config.bacrp_path}"
        result.update({"status": "error", "error": error})
        return result, error

    result["status"] = "ok"
    return result, None


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
        timeout_sec = int(request.get("timeout_sec", config.bacnet_timeout_sec))
    except (TypeError, ValueError):
        return None, "BACnet discovery request has invalid timeout_sec"

    port = CLOUD_BACNET_PORT
    base_result = {"bacnet_discover": True, "port": port}
    lock_fd = acquire_bacnet_runtime_lock(config)
    if lock_fd is None:
        return _deferred_result(base_result, config), BACNET_RUNTIME_BUSY

    env = os.environ.copy()
    env["BACNET_IP_PORT"] = str(port)

    try:
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
    finally:
        release_bacnet_runtime_lock(config, lock_fd)

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


def _normalize_object_type(raw_type: str) -> str | None:
    value = raw_type.strip().strip("()").strip().lower().replace("_", "-")
    if value.isdigit():
        return BACNET_OBJECT_TYPE_BY_ID.get(int(value))
    value = re.sub(r"(?<!^)([A-Z])", r"-\1", raw_type.strip()).lower().replace("_", "-")
    value = value.strip().strip("()").strip()
    return value if value in BACNET_LOAD_POINT_OBJECT_TYPES or value == "device" else None


def parse_bacnet_object_list(raw_output: str) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line or "error" in line.lower():
            continue

        matches = [
            (match.group(1), match.group(2))
            for match in re.finditer(r"\(?\b([A-Za-z][A-Za-z0-9_-]+|\d+)\s*[,=:]\s*(\d+)\)?", line)
        ]
        for raw_type, raw_instance in matches:
            object_type = _normalize_object_type(raw_type)
            if object_type is None or object_type == "device":
                continue
            object_instance = int(raw_instance)
            key = (object_type, object_instance)
            if key in seen:
                continue
            seen.add(key)
            points.append(
                {
                    "object_type": object_type,
                    "object_instance": object_instance,
                    "property_name": BACNET_PRESENT_VALUE,
                    "object_name": None,
                    "present_value": None,
                    "units": None,
                    "writable": None,
                }
            )
    return points


def validate_bacnet_load_points_request(request: dict[str, Any]) -> dict[str, object]:
    device_instance = _required_int(request, "device_instance")
    limit = request.get("limit", 250)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("limit must be an integer between 1 and 1000")

    object_types = request.get("object_types")
    normalized_types: list[str] | None = None
    if object_types is not None:
        if not isinstance(object_types, list) or not all(isinstance(item, str) for item in object_types):
            raise ValueError("object_types must be a list of strings")
        normalized_types = []
        for item in object_types:
            object_type = _normalize_object_type(item)
            if object_type is None or object_type == "device":
                raise ValueError(f"unsupported object_type: {item}")
            normalized_types.append(object_type)

    include_object_names = request.get("include_object_names", True)
    if not isinstance(include_object_names, bool):
        raise ValueError("include_object_names must be a boolean")

    return {
        "job_type": "bacnet_load_points",
        "device_instance": device_instance,
        "object_types": normalized_types,
        "limit": limit,
        "include_object_names": include_object_names,
        "bacnet_port": CLOUD_BACNET_PORT,
    }


def build_bacnet_read_args(config: AgentConfig, normalized_request: dict[str, object]) -> list[str]:
    return [
        config.bacrp_path,
        str(normalized_request["device_instance"]),
        str(normalized_request["object_type"]),
        str(normalized_request["object_instance"]),
        str(normalized_request["property_id"]),
    ]


def build_bacnet_load_points_args(config: AgentConfig, normalized_request: dict[str, object]) -> list[str]:
    device_instance = str(normalized_request["device_instance"])
    return [
        config.bacrp_path,
        device_instance,
        "device",
        device_instance,
        str(BACNET_OBJECT_LIST_PROPERTY_ID),
        "-2",
    ]


def build_bacnet_object_name_args(config: AgentConfig, device_instance: int, object_type: str, object_instance: int) -> list[str]:
    return [
        config.bacrp_path,
        str(device_instance),
        object_type,
        str(object_instance),
        str(BACNET_OBJECT_NAME_PROPERTY_ID),
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
    env["BACNET_IP_PORT"] = str(CLOUD_BACNET_PORT)
    args = build_bacnet_read_args(config, normalized)

    lock_fd = acquire_bacnet_runtime_lock(config)
    if lock_fd is None:
        return _deferred_result(normalized, config), BACNET_RUNTIME_BUSY

    try:
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
    finally:
        release_bacnet_runtime_lock(config, lock_fd)

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


def _parse_object_name(raw_output: str) -> str | None:
    lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    for line in lines:
        if "error" in line.lower():
            continue
        match = re.search(r"object[-_ ]name\s*(?:\([^)]*\))?\s*[:=]\s*(.+)$", line, flags=re.IGNORECASE)
        if match is not None:
            return _clean_candidate(match.group(1))
        if len(lines) == 1:
            return _clean_candidate(line)
    return None


def run_bacnet_load_points(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        normalized = validate_bacnet_load_points_request(request)
    except ValueError as exc:
        result = _failure_result({"job_type": "bacnet_load_points", "bacnet_port": CLOUD_BACNET_PORT}, str(exc))
        return result, str(exc)

    env = os.environ.copy()
    env["BACNET_IP_PORT"] = str(CLOUD_BACNET_PORT)
    args = build_bacnet_load_points_args(config, normalized)
    lock_fd = acquire_bacnet_runtime_lock(config)
    if lock_fd is None:
        return _deferred_result(normalized, config), BACNET_RUNTIME_BUSY

    try:
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
            error = f"BACnet object-list read timed out after {config.bacnet_timeout_sec} seconds"
            return _failure_result(normalized, error, raw_output or None), error
        except OSError as exc:
            error = f"BACnet object-list read failed to start: {exc}"
            return _failure_result(normalized, error), error

        raw_output = completed.stdout
        combined_output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            error = f"BACnet object-list read failed: {detail}"
            return _failure_result(normalized, error, combined_output or None), error

        points = parse_bacnet_object_list(raw_output)
        if normalized["object_types"] is not None:
            allowed = set(normalized["object_types"])
            points = [point for point in points if point["object_type"] in allowed]
        points = points[: int(normalized["limit"])]

        if normalized["include_object_names"]:
            device_instance = int(normalized["device_instance"])
            for point in points:
                name_args = build_bacnet_object_name_args(
                    config,
                    device_instance,
                    str(point["object_type"]),
                    int(point["object_instance"]),
                )
                try:
                    name_completed = subprocess.run(
                        name_args,
                        capture_output=True,
                        check=False,
                        env=env,
                        text=True,
                        timeout=config.bacnet_timeout_sec,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    continue
                if name_completed.returncode == 0:
                    point["object_name"] = _parse_object_name(name_completed.stdout)
    finally:
        release_bacnet_runtime_lock(config, lock_fd)

    result = dict(normalized)
    result.update(
        {
            "status": "ok",
            "points": points,
            "point_count": len(points),
            "raw_output": raw_output,
        }
    )
    return result, None
