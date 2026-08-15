"""Cloud Phase 2 template persistence and single-binding authority."""
from uuid import uuid4

from test_api import client, create_gateway_token, create_operator_user, reset_database, user_headers


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


def test_explicit_device_configuration_persists_manual_bindings_for_both_bms_renderers() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    device = client.post(
        f"/api/ui/gateways/{gateway_id}/devices",
        headers=headers,
        json={"device_instance": 1101, "device_name": "SE8650U0Bxx-1"},
    ).json()
    points = {
        name: client.post(
            f"/api/ui/devices/{device['id']}/points",
            headers=headers,
            json={"object_type": object_type, "object_instance": instance, "object_name": name, "present_value": value},
        ).json()
        for name, object_type, instance, value in (
            ("Room Temperature", "analog-value", 1, "72.1"),
            ("UI22 Supply Temperature", "analog-value", 22, "55.0"),
            ("Effective Occupancy", "multi-state-input", 3, "Occupied"),
        )
    }
    saved = client.put(
        f"/api/ui/devices/{device['id']}/configuration",
        headers=headers,
        json={
            "group_name": "HVAC",
            "template_key": "rtu",
            "point_roles": {
                points["Room Temperature"]["id"]: "space_temp",
                points["UI22 Supply Temperature"]["id"]: "supply_air_temp",
                points["Effective Occupancy"]["id"]: "occupancy_mode",
            },
        },
    )
    assert saved.status_code == 200
    assert {point["logical_role"] for point in saved.json()["points"]} == {"space_temp", "supply_air_temp", "occupancy_mode"}
    tree = client.get(f"/api/ui/gateways/{gateway_id}/tree", headers=headers).json()
    roles = {point["logical_role"]: point["present_value"] for point in tree["points"]}
    assert roles == {"space_temp": "72.1", "supply_air_temp": "55.0", "occupancy_mode": "Occupied"}
    workspace = client.get(f"/gateways/{gateway_id}")
    graphic = client.get(f"/gateways/{gateway_id}/devices/{device['id']}")
    assert "equipment-values" in workspace.text
    assert "rtu-graphic-grid" in graphic.text
    assert "pointValue(points.get(role))" in workspace.text
    assert "pointValue(bound.get(role))" in graphic.text


def test_mapping_template_applies_own_points_to_three_devices() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    mapping = client.post("/api/ui/mapping-templates", headers=headers, json={"name": "SE8650 RTU", "graphic_template_key": "rtu", "rules": [{"logical_role": "space_temp", "match_field": "object_name", "match_value": "Room Temperature", "object_type": "analog-value", "required": True}, {"logical_role": "occupancy_mode", "match_field": "object_name", "match_value": "Effective Occupancy", "object_type": "multi-state-input", "required": False}]}).json()
    ids = []
    for index in range(1, 4):
        device = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"device_instance": 1100 + index, "device_name": f"SE8650U0Bxx-{index}"}).json()
        client.post(f"/api/ui/devices/{device['id']}/points", headers=headers, json={"object_type": "analog-value", "object_instance": index, "object_name": f" Room Temperature ", "present_value": str(70 + index)})
        client.post(f"/api/ui/devices/{device['id']}/points", headers=headers, json={"object_type": "multi-state-input", "object_instance": 20 + index, "object_name": "Effective Occupancy", "present_value": "Occupied"})
        assert client.put(f"/api/ui/devices/{device['id']}/configuration", headers=headers, json={"group_name": "HVAC", "template_key": "rtu", "mapping_template_id": mapping["id"]}).status_code == 200
        result = client.post(f"/api/ui/devices/{device['id']}/mapping-template/{mapping['id']}/apply", headers=headers)
        assert result.status_code == 200 and result.json()["matched"] == 2
        ids.append(device["id"])
    tree = client.get(f"/api/ui/gateways/{gateway_id}/tree", headers=headers).json()
    bound_ids = {point["saved_device_id"] for point in tree["points"] if point["logical_role"] == "space_temp"}
    assert bound_ids == set(ids)
