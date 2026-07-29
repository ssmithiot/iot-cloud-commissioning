"""Unit tests for the fixed USB-485 BACnet MS/TP router helper."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[2] / "deploy" / "iot-cx-configure-mstp-router"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("mstp_router_helper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def valid_config() -> dict[str, object]:
    return {"enabled": True, "interface": "eth0", "ip_port": 47809, "bbmd_port": 47809, "ip_network": 1, "serial_device": "/dev/serial/by-id/usb-FTDI_FT232R", "baud": 38400, "mstp_mac": 5, "mstp_network": 202, "max_master": 127, "max_info_frames": 1}


def test_config_validates_approved_usb_serial_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper()
    config_path = tmp_path / "bacnet_router.json"
    config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
    monkeypatch.setattr(helper, "CONFIG", config_path)
    assert helper.config() == valid_config()


@pytest.mark.parametrize(("field", "value", "message"), [("enabled", "false", "router enabled must be true or false"), ("interface", "eth0;id", "invalid gateway interface"), ("serial_device", "/tmp/ttyUSB0", "invalid serial device"), ("baud", 4800, "unsupported MS/TP baud"), ("mstp_mac", 128, "MS/TP MAC must be between 0 and 127"), ("bbmd_port", 47814, "BACnet BBMD port must match")])
def test_config_rejects_unsafe_or_invalid_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object, message: str) -> None:
    helper = load_helper()
    payload = valid_config()
    payload[field] = value
    config_path = tmp_path / "bacnet_router.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(helper, "CONFIG", config_path)
    with pytest.raises(ValueError, match=message):
        helper.config()


def test_unit_text_has_only_validated_fixed_router_values() -> None:
    helper = load_helper()
    unit = helper.unit_text(valid_config())
    assert "Environment=BACNET_MSTP_IFACE=/dev/serial/by-id/usb-FTDI_FT232R" in unit
    assert "Environment=BACNET_IP_PORT=47809" in unit
    assert "Environment=BACNET_BBMD_PORT=47809" in unit
    assert "ExecStart=/home/swadmin/bacnet-stack/bin/router-mstp" in unit
    assert "User=root" in unit


def test_apply_refuses_to_replace_an_unmanaged_router_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper()
    config_path = tmp_path / "bacnet_router.json"
    config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
    unmanaged_unit = tmp_path / "iot-cx-mstp-router.service"
    unmanaged_unit.write_text("[Service]\nExecStart=/existing/router\n", encoding="utf-8")
    monkeypatch.setattr(helper, "CONFIG", config_path)
    monkeypatch.setattr(helper, "UNIT", unmanaged_unit)

    with pytest.raises(RuntimeError, match="refusing to replace unmanaged router unit"):
        helper.apply()
