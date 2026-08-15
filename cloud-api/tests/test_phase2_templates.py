"""Cloud Phase 2 template persistence and single-binding authority."""
from uuid import uuid4

from test_api import client, create_gateway_token, create_operator_user, user_headers


def test_template_and_logical_role_binding_are_persisted_and_unique() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    group = client.post(f"/api/ui/gateways/{gateway_id}/groups", headers=headers, json={"name": "HVAC"}).json()
    first = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"group_id": group["id"], "device_instance": 1, "template_key": "rtu"})
    second = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"group_id": group["id"], "device_instance": 2, "template_key": "ahu"})
    assert first.status_code == 200 and first.json()["template_key"] == "rtu"
    assert second.status_code == 200 and second.json()["template_key"] == "ahu"
    first_point = client.post(f"/api/ui/devices/{first.json()['id']}/points", headers=headers, json={"object_type": "analog-input", "object_instance": 1}).json()
    duplicate = client.post(f"/api/ui/devices/{first.json()['id']}/points", headers=headers, json={"object_type": "analog-input", "object_instance": 2}).json()
    other_device = client.post(f"/api/ui/devices/{second.json()['id']}/points", headers=headers, json={"object_type": "analog-input", "object_instance": 1}).json()
    assert client.patch(f"/api/ui/points/{first_point['id']}", headers=headers, json={"logical_role": "space_temp"}).status_code == 200
    assert client.patch(f"/api/ui/points/{duplicate['id']}", headers=headers, json={"logical_role": "space_temp"}).status_code == 409
    assert client.patch(f"/api/ui/points/{other_device['id']}", headers=headers, json={"logical_role": "space_temp"}).status_code == 200
    assert client.patch(f"/api/ui/devices/{first.json()['id']}", headers=headers, json={"template_key": "unknown"}).status_code == 422


def test_template_category_and_binding_safety() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    create_gateway_token(gateway_id)
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    lighting = client.post(f"/api/ui/gateways/{gateway_id}/groups", headers=headers, json={"name": "Lighting"}).json()
    hvac = client.post(f"/api/ui/gateways/{gateway_id}/groups", headers=headers, json={"name": "HVAC"}).json()
    assert client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"group_id": lighting["id"], "device_instance": 1, "template_key": "rtu"}).status_code == 422
    device = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"group_id": hvac["id"], "device_instance": 2, "template_key": "rtu"}).json()
    point = client.post(f"/api/ui/devices/{device['id']}/points", headers=headers, json={"object_type": "analog-input", "object_instance": 1}).json()
    assert client.patch(f"/api/ui/points/{point['id']}", headers=headers, json={"logical_role": "return_air_temp"}).status_code == 200
    assert client.patch(f"/api/ui/devices/{device['id']}", headers=headers, json={"template_key": "minisplit"}).status_code == 409
    unconfigured = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"group_id": hvac["id"], "device_instance": 3}).json()
    unconfigured_point = client.post(f"/api/ui/devices/{unconfigured['id']}/points", headers=headers, json={"object_type": "analog-input", "object_instance": 1}).json()
    assert client.patch(f"/api/ui/points/{unconfigured_point['id']}", headers=headers, json={"logical_role": "space_temp"}).status_code == 422


def test_device_points_page_uses_device_scoped_mirrored_table() -> None:
    response = client.get("/gateways/GW001/devices/device-1/points")
    assert response.status_code == 200
    assert 'data-device-id="device-1"' in response.text
    assert "point.saved_device_id === deviceId" in response.text
    assert "View All Points" in response.text
