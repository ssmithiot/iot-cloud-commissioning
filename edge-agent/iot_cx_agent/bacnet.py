import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
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
    "schedule",
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
BACNET_POINT_LOAD_BATCH_SIZE = 40
BACNET_OBJECT_LIST_INDEX_BLOCK_SIZE = 40
BACNET_RUNTIME_BUSY = "bacnet_runtime_busy"


def resolved_bacnet_port(config: AgentConfig) -> int:
    return config.bacnet_default_port


def _runtime_busy_message(port: int) -> str:
    return f"BACnet runtime is busy. Another local BACnet command is already using UDP {port}."


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_pid(lock_path: Path) -> int | None:
    try:
        first_line = lock_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (FileNotFoundError, IndexError, OSError):
        return None
    try:
        return int(first_line)
    except ValueError:
        return None


def _lock_is_stale(lock_path: Path, stale_after_sec: float) -> bool:
    pid = _lock_pid(lock_path)
    try:
        age_sec = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return True
    if pid is None or not _pid_is_running(pid):
        return True
    return age_sec > stale_after_sec


@dataclass
class BacnetRuntimeLock:
    config: AgentConfig
    port: int

    def __post_init__(self) -> None:
        self.path = self.config.bacnet_lock_path_for_port(self.port)
        self.fd: int | None = None

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.config.bacnet_lock_timeout_sec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                content = f"{os.getpid()}\n{time.time():.3f}\nport={self.port}\n"
                os.write(self.fd, content.encode("utf-8"))
                return True
            except FileExistsError:
                if _lock_is_stale(self.path, self.config.bacnet_lock_stale_sec):
                    try:
                        self.path.unlink()
                        continue
                    except FileNotFoundError:
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            if _lock_pid(self.path) == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "BacnetRuntimeLock":
        acquired = self.acquire()
        if not acquired:
            raise TimeoutError(_runtime_busy_message(self.port))
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def bacnet_runtime_lock_held(config: AgentConfig, port: int | None = None) -> bool:
    resolved_port = resolved_bacnet_port(config) if port is None else port
    lock_path = config.bacnet_lock_path_for_port(resolved_port)
    if not lock_path.exists():
        return False
    if _lock_is_stale(lock_path, config.bacnet_lock_stale_sec):
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        return False
    return True


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
    port = int(result.get("bacnet_port", result.get("port", resolved_bacnet_port(config))))
    result.update(
        {
            "status": "deferred",
            "error": BACNET_RUNTIME_BUSY,
            "message": _runtime_busy_message(port),
            "lock_path": str(config.bacnet_lock_path_for_port(port)),
            "lock_held": True,
        }
    )
    return result


def run_bacnet_runtime_check(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        timeout_sec = int(request.get("timeout_sec", config.bacnet_timeout_sec))
    except (TypeError, ValueError):
        timeout_sec = config.bacnet_timeout_sec

    port = resolved_bacnet_port(config)
    bacwi = _command_status(config.bacwi_path)
    bacrp = _command_status(config.bacrp_path)
    bacrpm = _command_status(config.bacrpm_path)
    result = {
        "job_type": "bacnet_runtime_check",
        "bacnet_port": port,
        "bacnet_router_profile": config.bacnet_router_profile,
        "timeout_sec": timeout_sec,
        "lock_path": str(config.bacnet_lock_path_for_port(port)),
        "lock_held": bacnet_runtime_lock_held(config, port),
        "bacwi_configured_path": bacwi["configured_path"],
        "bacwi_resolved_path": bacwi["resolved_path"],
        "bacwi_exists": bacwi["exists"],
        "bacwi_executable": bacwi["executable"],
        "bacrp_configured_path": bacrp["configured_path"],
        "bacrp_resolved_path": bacrp["resolved_path"],
        "bacrp_exists": bacrp["exists"],
        "bacrp_executable": bacrp["executable"],
        "bacrpm_configured_path": bacrpm["configured_path"],
        "bacrpm_resolved_path": bacrpm["resolved_path"],
        "bacrpm_exists": bacrpm["exists"],
        "bacrpm_executable": bacrpm["executable"],
    }

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

    port = resolved_bacnet_port(config)
    base_result = {"bacnet_discover": True, "port": port, "bacnet_router_profile": config.bacnet_router_profile}

    env = os.environ.copy()
    env["BACNET_IP_PORT"] = str(port)

    try:
        lock = BacnetRuntimeLock(config, port)
        if not lock.acquire():
            return _deferred_result(base_result, config), BACNET_RUNTIME_BUSY
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
        if "lock" in locals():
            lock.release()

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
        raise ValueError(f"object_type received {object_type!r}; must be one of: {allowed}")

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


def validate_bacnet_read_bulk_request(request: dict[str, Any]) -> dict[str, object]:
    device_instance = _required_int(request, "device_instance")
    points = request.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("points must be a non-empty list")
    normalized_points: list[dict[str, object]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"points[{index}] must be an object")
        object_instance = _required_int(point, "object_instance")
        object_type = point.get("object_type")
        if not isinstance(object_type, str) or object_type not in BACNET_READ_OBJECT_TYPES:
            allowed = ", ".join(sorted(BACNET_READ_OBJECT_TYPES))
            raise ValueError(
                f"points[{index}].object_type received {object_type!r}; must be one of: {allowed}"
            )
        saved_point_id = point.get("saved_point_id")
        normalized_points.append(
            {
                "saved_point_id": str(saved_point_id) if saved_point_id is not None else None,
                "object_type": object_type,
                "object_instance": object_instance,
                "object_name": point.get("object_name") if isinstance(point.get("object_name"), str) else None,
                "property": BACNET_PRESENT_VALUE,
                "property_id": BACNET_PRESENT_VALUE_PROPERTY_ID,
            }
        )

    return {
        "job_type": "bacnet_read_bulk",
        "device_instance": device_instance,
        "property": BACNET_PRESENT_VALUE,
        "property_id": BACNET_PRESENT_VALUE_PROPERTY_ID,
        "points": normalized_points,
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


def validate_bacnet_load_points_request(config: AgentConfig, request: dict[str, Any]) -> dict[str, object]:
    device_instance = _required_int(request, "device_instance")
    limit = request.get("limit", 250)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    name_limit = request.get("name_limit", min(limit, 50))
    if isinstance(name_limit, bool) or not isinstance(name_limit, int) or name_limit < 0 or name_limit > limit:
        raise ValueError("name_limit must be an integer between 0 and limit")

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
        "name_limit": name_limit,
        "include_object_names": include_object_names,
        "bacnet_port": resolved_bacnet_port(config),
    }


def build_bacnet_read_args(config: AgentConfig, normalized_request: dict[str, object]) -> list[str]:
    return [
        config.bacrp_path,
        str(normalized_request["device_instance"]),
        str(normalized_request["object_type"]),
        str(normalized_request["object_instance"]),
        str(normalized_request["property_id"]),
    ]


def build_bacnet_load_points_args(
    config: AgentConfig,
    normalized_request: dict[str, object],
    array_index: int | None = None,
) -> list[str]:
    device_instance = str(normalized_request["device_instance"])
    args = [
        config.bacrp_path,
        device_instance,
        "device",
        device_instance,
        str(BACNET_OBJECT_LIST_PROPERTY_ID),
    ]
    if array_index is not None:
        args.append(str(array_index))
    return args


def build_bacnet_object_name_args(config: AgentConfig, device_instance: int, object_type: str, object_instance: int) -> list[str]:
    return [
        config.bacrp_path,
        str(device_instance),
        object_type,
        str(object_instance),
        str(BACNET_OBJECT_NAME_PROPERTY_ID),
    ]


def build_bacnet_rpm_point_batch_args(
    config: AgentConfig,
    device_instance: int,
    points: list[dict[str, object]],
) -> list[str]:
    args = [config.bacrpm_path, str(device_instance)]
    for point in points:
        args.extend([str(point["object_type"]), str(point["object_instance"]), str(BACNET_OBJECT_NAME_PROPERTY_ID)])
    return args


def build_bacnet_rpm_value_batch_args(
    config: AgentConfig,
    device_instance: int,
    points: list[dict[str, object]],
) -> list[str]:
    args = [config.bacrpm_path, str(device_instance)]
    for point in points:
        args.extend([str(point["object_type"]), str(point["object_instance"]), str(BACNET_PRESENT_VALUE_PROPERTY_ID)])
    return args


def build_bacnet_rpm_point_args(
    config: AgentConfig,
    device_instance: int,
    point: dict[str, object],
) -> list[str]:
    return [
        config.bacrpm_path,
        str(device_instance),
        str(point["object_type"]),
        str(point["object_instance"]),
        str(BACNET_OBJECT_NAME_PROPERTY_ID),
    ]


def build_bacnet_rpm_object_list_args(config: AgentConfig, device_instance: int, start_index: int, end_index: int) -> list[str]:
    prop_list = ",".join(f"{BACNET_OBJECT_LIST_PROPERTY_ID}[{index}]" for index in range(start_index, end_index + 1))
    return [
        config.bacrpm_path,
        str(device_instance),
        "device",
        str(device_instance),
        prop_list,
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
    port = resolved_bacnet_port(config)
    env["BACNET_IP_PORT"] = str(port)
    args = build_bacnet_read_args(config, normalized)
    normalized["bacnet_port"] = port
    normalized["bacnet_router_profile"] = config.bacnet_router_profile

    lock = BacnetRuntimeLock(config, port)
    if not lock.acquire():
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
        lock.release()

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


def run_bacnet_read_bulk(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        normalized = validate_bacnet_read_bulk_request(request)
    except ValueError as exc:
        result = _failure_result({"job_type": "bacnet_read_bulk", "property": BACNET_PRESENT_VALUE, "property_id": 85}, str(exc))
        return result, str(exc)

    env = os.environ.copy()
    port = resolved_bacnet_port(config)
    env["BACNET_IP_PORT"] = str(port)
    normalized["bacnet_port"] = port
    normalized["bacnet_router_profile"] = config.bacnet_router_profile

    rpm_status = _command_status(config.bacrpm_path)
    if not rpm_status["executable"]:
        error = f"BACnet RPM command is not executable: {config.bacrpm_path}"
        return _failure_result(normalized, error), error

    lock = BacnetRuntimeLock(config, port)
    if not lock.acquire():
        return _deferred_result(normalized, config), BACNET_RUNTIME_BUSY

    values: list[dict[str, object]] = []
    errors: list[str] = []
    raw_outputs: list[str] = []
    fallback_count = 0
    try:
        for chunk in _chunks(list(normalized["points"]), BACNET_POINT_LOAD_BATCH_SIZE):
            args = build_bacnet_rpm_value_batch_args(config, int(normalized["device_instance"]), chunk)
            completed, error = _run_command(args, config, env, "BACnet value RPM batch read")
            parsed: dict[tuple[str, int], tuple[object, str]] = {}
            if error is not None or completed is None:
                errors.append(error or "BACnet value RPM batch read failed")
            else:
                raw_output = _combined_output(completed)
                if raw_output:
                    raw_outputs.append(raw_output)
                parsed = parse_bacnet_rpm_present_values(completed.stdout)
            for point in chunk:
                key = (str(point["object_type"]), int(point["object_instance"]))
                parsed_value = parsed.get(key)
                if parsed_value is None:
                    fallback_request = {
                        "job_type": "bacnet_read",
                        "device_instance": normalized["device_instance"],
                        "object_type": point["object_type"],
                        "object_instance": point["object_instance"],
                        "property": BACNET_PRESENT_VALUE,
                        "property_id": BACNET_PRESENT_VALUE_PROPERTY_ID,
                    }
                    fallback_args = build_bacnet_read_args(config, fallback_request)
                    fallback_completed, fallback_error = _run_command(
                        fallback_args,
                        config,
                        env,
                        "BACnet value single-read fallback",
                    )
                    fallback_count += 1
                    if fallback_error is not None or fallback_completed is None:
                        values.append(
                            {
                                "saved_point_id": point.get("saved_point_id"),
                                "object_type": point["object_type"],
                                "object_instance": point["object_instance"],
                                "status": "missing",
                                "error": fallback_error or "present-value not returned",
                                "read_source": "single-fallback",
                            }
                        )
                        continue
                    value, raw_value = parse_bacnet_read_value(fallback_completed.stdout)
                    if raw_value is None:
                        values.append(
                            {
                                "saved_point_id": point.get("saved_point_id"),
                                "object_type": point["object_type"],
                                "object_instance": point["object_instance"],
                                "status": "missing",
                                "error": "single-read fallback did not return present-value",
                                "read_source": "single-fallback",
                            }
                        )
                        continue
                    raw_output = _combined_output(fallback_completed)
                    if raw_output:
                        raw_outputs.append(raw_output)
                    parsed_value = (value, raw_value)
                    read_source = "single-fallback"
                else:
                    read_source = "rpm-bulk"
                value, raw_value = parsed_value
                values.append(
                    {
                        "saved_point_id": point.get("saved_point_id"),
                        "object_type": point["object_type"],
                        "object_instance": point["object_instance"],
                        "value": value,
                        "raw_value": raw_value,
                        "status": "ok",
                        "read_source": read_source,
                    }
                )
    finally:
        lock.release()

    result = dict(normalized)
    result.update(
        {
            "status": "ok" if not errors else "partial",
            "read_mode": "rpm-bulk",
            "requested_count": len(normalized["points"]),
            "value_count": len([item for item in values if item.get("status") == "ok"]),
            "single_read_fallback_count": fallback_count,
            "values": values,
        }
    )
    if raw_outputs:
        result["raw_outputs"] = raw_outputs
    if errors:
        result["errors"] = errors
    if not any(item.get("status") == "ok" for item in values):
        error = "; ".join(errors) if errors else "BACnet bulk read returned no point values"
        return _failure_result(result, error), error
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


def parse_bacnet_rpm_object_names(raw_output: str) -> dict[tuple[str, int], str]:
    names: dict[tuple[str, int], str] = {}
    current: tuple[str, int] | None = None
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line or line in {"{", "}"} or line.lower().startswith("device #") or "error" in line.lower():
            continue

        object_match = (
            re.match(r"^\(?([a-z]+(?:-[a-z]+)*)\s*,\s*(\d+)\)?$", line, flags=re.IGNORECASE)
            or re.match(r"^([a-z]+(?:-[a-z]+)*)\s+#?(\d+)\s*$", line, flags=re.IGNORECASE)
        )
        if object_match is not None:
            object_type = _normalize_object_type(object_match.group(1))
            if object_type is not None:
                current = (object_type, int(object_match.group(2)))
            continue

        name_match = re.match(r"^(?:object[-_ ]name|77)\s*[:=]\s*(.*)$", line, flags=re.IGNORECASE)
        if name_match is not None and current is not None:
            names[current] = _clean_candidate(name_match.group(1))
            current = None
            continue
    return names


def parse_bacnet_rpm_present_values(raw_output: str) -> dict[tuple[str, int], tuple[object, str]]:
    values: dict[tuple[str, int], tuple[object, str]] = {}
    current: tuple[str, int] | None = None
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line or line in {"{", "}"} or line.lower().startswith("device #"):
            continue

        object_match = (
            re.match(r"^\(?([a-z]+(?:-[a-z]+)*)\s*,\s*(\d+)\)?$", line, flags=re.IGNORECASE)
            or re.match(r"^([a-z]+(?:-[a-z]+)*)\s+#?(\d+)\s*$", line, flags=re.IGNORECASE)
        )
        if object_match is not None:
            object_type = _normalize_object_type(object_match.group(1))
            if object_type is not None:
                current = (object_type, int(object_match.group(2)))
            continue

        value_match = re.match(r"^(?:present[-_ ]value|85)\s*[:=]\s*(.*)$", line, flags=re.IGNORECASE)
        if value_match is not None and current is not None:
            raw_value = _clean_candidate(value_match.group(1))
            values[current] = (_coerce_bacnet_value(raw_value), raw_value)
            current = None
            continue
    return values


def _parse_rpm_point_values(raw_output: str, requested_points: list[dict[str, object]]) -> list[dict[str, object]]:
    parsed_points = [dict(point) for point in requested_points]
    names = parse_bacnet_rpm_object_names(raw_output)
    for point in parsed_points:
        point["object_name"] = names.get((str(point["object_type"]), int(point["object_instance"])), point.get("object_name"))
    return parsed_points


def _parse_array_length(raw_output: str) -> int | None:
    candidates = re.findall(r"\b\d+\b", raw_output)
    if not candidates:
        return None
    return int(candidates[-1])


def _run_bacrp(
    args: list[str],
    config: AgentConfig,
    env: dict[str, str],
    description: str,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
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
        return None, f"BACnet read command not found: {config.bacrp_path}"
    except subprocess.TimeoutExpired:
        return None, f"{description} timed out after {config.bacnet_timeout_sec} seconds"
    except OSError as exc:
        return None, f"{description} failed to start: {exc}"

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        return completed, f"{description} failed: {detail}"
    return completed, None


def _run_command(
    args: list[str],
    config: AgentConfig,
    env: dict[str, str],
    description: str,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
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
        return None, f"{description} command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return None, f"{description} timed out after {config.bacnet_timeout_sec} seconds"
    except OSError as exc:
        return None, f"{description} failed to start: {exc}"

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        return completed, f"{description} failed: {detail}"
    return completed, None


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)


def _chunks(items: list[dict[str, object]], size: int) -> list[list[dict[str, object]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _point_key(point: dict[str, object]) -> tuple[object, int]:
    return point["object_type"], int(point["object_instance"])


def _point_has_enrichment(point: dict[str, object]) -> bool:
    return point.get("object_name") is not None


def _enrich_points_with_rpm(
    config: AgentConfig,
    env: dict[str, str],
    device_instance: int,
    points: list[dict[str, object]],
    max_points: int,
) -> tuple[list[dict[str, object]], str]:
    rpm_status = _command_status(config.bacrpm_path)
    if not rpm_status["executable"]:
        return points, "bacrp"

    enriched = [dict(point) for point in points]
    targets = enriched[:max_points]
    used_rpm = False
    for chunk in _chunks(targets, BACNET_POINT_LOAD_BATCH_SIZE):
        batch_args = build_bacnet_rpm_point_batch_args(config, device_instance, chunk)
        batch_completed, batch_error = _run_command(batch_args, config, env, "BACnet point RPM batch read")
        if batch_error is None and batch_completed is not None:
            parsed = _parse_rpm_point_values(batch_completed.stdout, chunk)
            for parsed_point in parsed:
                key = _point_key(parsed_point)
                for point in enriched:
                    if _point_key(point) == key:
                        point.update(parsed_point)
                        break
            used_rpm = True
            missing = [point for point in chunk if not _point_has_enrichment(point)]
        else:
            missing = chunk

        for rpm_point in missing:
            rpm_args = build_bacnet_rpm_point_args(config, device_instance, rpm_point)
            completed, error = _run_command(rpm_args, config, env, "BACnet point RPM read")
            if error is not None or completed is None:
                continue
            parsed = _parse_rpm_point_values(completed.stdout, [rpm_point])
            key = _point_key(parsed[0])
            for point in enriched:
                if _point_key(point) == key:
                    point.update(parsed[0])
                    break
            used_rpm = True

    return enriched, "rpm" if used_rpm else "bacrp"


def _read_object_list_with_rpm_blocks(
    config: AgentConfig,
    env: dict[str, str],
    normalized: dict[str, object],
) -> tuple[list[dict[str, object]], list[str], str]:
    rpm_status = _command_status(config.bacrpm_path)
    if not rpm_status["executable"]:
        return [], [], "bacrp"

    device_instance = int(normalized["device_instance"])
    limit = int(normalized["limit"])
    points: list[dict[str, object]] = []
    raw_outputs: list[str] = []
    seen: set[tuple[str, int]] = set()
    for start_index in range(1, limit + 1, BACNET_OBJECT_LIST_INDEX_BLOCK_SIZE):
        end_index = min(limit, start_index + BACNET_OBJECT_LIST_INDEX_BLOCK_SIZE - 1)
        args = build_bacnet_rpm_object_list_args(config, device_instance, start_index, end_index)
        completed, error = _run_command(args, config, env, "BACnet object-list RPM block read")
        if error is not None or completed is None:
            continue
        raw_outputs.append(completed.stdout)
        for point in parse_bacnet_object_list(completed.stdout):
            key = (str(point["object_type"]), int(point["object_instance"]))
            if key in seen:
                continue
            seen.add(key)
            points.append(point)
    return points[:limit], raw_outputs, "rpm-index-blocks" if points else "bacrp"


def _enrich_points_with_bacrp_names(
    config: AgentConfig,
    env: dict[str, str],
    device_instance: int,
    points: list[dict[str, object]],
    max_points: int,
) -> list[dict[str, object]]:
    enriched = [dict(point) for point in points]
    for point in enriched[:max_points]:
        name_args = build_bacnet_object_name_args(
            config,
            device_instance,
            str(point["object_type"]),
            int(point["object_instance"]),
        )
        completed, error = _run_bacrp(name_args, config, env, "BACnet object-name read")
        if error is None and completed is not None:
            point["object_name"] = _parse_object_name(completed.stdout)
    return enriched


def run_bacnet_load_points(config: AgentConfig, request: dict[str, Any]) -> tuple[dict[str, object], str | None]:
    try:
        normalized = validate_bacnet_load_points_request(config, request)
    except ValueError as exc:
        result = _failure_result({"job_type": "bacnet_load_points", "bacnet_port": resolved_bacnet_port(config)}, str(exc))
        return result, str(exc)

    env = os.environ.copy()
    port = resolved_bacnet_port(config)
    env["BACNET_IP_PORT"] = str(port)
    normalized["bacnet_port"] = port
    normalized["bacnet_router_profile"] = config.bacnet_router_profile
    lock = BacnetRuntimeLock(config, port)
    if not lock.acquire():
        return _deferred_result(normalized, config), BACNET_RUNTIME_BUSY

    try:
        points, raw_outputs, read_mode = _read_object_list_with_rpm_blocks(config, env, normalized)
        object_count: int | None = len(points) if points else None

        if not points:
            read_mode = "indexed"
            count_args = build_bacnet_load_points_args(config, normalized, array_index=0)
            count_completed, count_error = _run_bacrp(count_args, config, env, "BACnet object-list count read")
            if count_error is not None:
                raw_output = _combined_output(count_completed) if count_completed is not None else ""
                return _failure_result(normalized, count_error, raw_output or None), count_error

            assert count_completed is not None
            raw_outputs = [count_completed.stdout]
            object_count = _parse_array_length(count_completed.stdout)
            if object_count is None:
                error = "BACnet object-list count read did not return an array length"
                raw_output = _combined_output(count_completed)
                return _failure_result(normalized, error, raw_output or None), error

            max_items = min(object_count, int(normalized["limit"]))
            points = []
            for array_index in range(1, max_items + 1):
                item_args = build_bacnet_load_points_args(config, normalized, array_index=array_index)
                item_completed, item_error = _run_bacrp(item_args, config, env, "BACnet object-list item read")
                if item_error is not None:
                    raw_output = _combined_output(item_completed) if item_completed is not None else ""
                    return _failure_result(normalized, item_error, raw_output or None), item_error
                assert item_completed is not None
                raw_outputs.append(item_completed.stdout)
                points.extend(parse_bacnet_object_list(item_completed.stdout))

        if normalized["object_types"] is not None:
            allowed = set(normalized["object_types"])
            points = [point for point in points if point["object_type"] in allowed]
        points = points[: int(normalized["limit"])]
        object_count = len(points)

        enrichment_mode = "none"
        if normalized["include_object_names"]:
            device_instance = int(normalized["device_instance"])
            points, enrichment_mode = _enrich_points_with_rpm(
                config,
                env,
                device_instance,
                points,
                int(normalized["name_limit"]),
            )
            if enrichment_mode == "bacrp":
                points = _enrich_points_with_bacrp_names(
                    config,
                    env,
                    device_instance,
                    points,
                    int(normalized["name_limit"]),
                )
    finally:
        lock.release()

    raw_output = "\n".join(output.strip() for output in raw_outputs if output.strip())
    result = dict(normalized)
    result.update(
        {
            "status": "ok",
            "points": points,
            "point_count": len(points),
            "object_count": object_count,
            "read_mode": read_mode,
            "enrichment_mode": enrichment_mode,
            "batch_size": BACNET_POINT_LOAD_BATCH_SIZE,
            "raw_output": raw_output,
        }
    )
    return result, None
