"""Cloud Phase 2 template persistence and single-binding authority."""
import csv
import io
from uuid import uuid4

from test_api import client, create_gateway_token, create_operator_user, reset_database, user_headers


LEGACY_CSV_COLUMNS = ["template_name", "graphic_template", "logical_role", "match_field", "match_value", "object_type", "required"]
CSV_COLUMNS = ["template_name", "graphic_template", "logical_role", "display_label", "match_field", "match_value", "object_type", "required"]
EXAMPLE_RULES = [
    ("space_temp", "Room Temperature", "analog-value"),
    ("supply_air_temp", "UI22 Supply Temperature", "analog-value"),
    ("occupancy_mode", "Effective Occupancy", "multi-state-input"),
    ("cool_stage_1", "Y1 Status", "binary-output"),
    ("cool_stage_2", "Y2 Status", "binary-output"),
    ("heat_stage_1", "W1 Status", "binary-output"),
    ("heat_stage_2", "W2/OB Status", "binary-output"),
]


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
    assert "rtu-grid" in graphic.text
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


def test_role_first_configuration_persists_labels_blocks_duplicate_points_and_uses_all_points_authority() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    device = client.post(
        f"/api/ui/gateways/{gateway_id}/devices",
        headers=headers,
        json={"device_instance": 8650, "device_name": "SE8650"},
    ).json()
    created = []
    for instance in range(32):
        created.append(
            client.post(
                f"/api/ui/devices/{device['id']}/points",
                headers=headers,
                json={
                    "object_type": "analog-value" if instance < 30 else "binary-output",
                    "object_instance": instance,
                    "object_name": "Room Temperature" if instance == 0 else f"Point {instance}",
                    "present_value": str(70 + instance / 10),
                },
            ).json()
        )
    room, supply = created[0], created[1]

    duplicate = client.put(
        f"/api/ui/devices/{device['id']}/configuration",
        headers=headers,
        json={
            "group_name": "HVAC",
            "template_key": "rtu",
            "role_points": {"space_temp": room["id"], "return_air_temp": room["id"]},
            "role_display_labels": {"space_temp": "Space Temperature", "return_air_temp": "Return Air Temperature"},
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Room Temperature is already assigned to Space Temperature and cannot also be assigned to Return Air Temperature."

    saved = client.put(
        f"/api/ui/devices/{device['id']}/configuration",
        headers=headers,
        json={
            "group_name": "HVAC",
            "template_key": "rtu",
            "role_points": {"space_temp": room["id"], "supply_air_temp": supply["id"]},
            "role_display_labels": {"space_temp": "  Space Temperature  ", "supply_air_temp": " Sales Floor Supply Air "},
        },
    )
    assert saved.status_code == 200, saved.text
    by_role = {point["logical_role"]: point for point in saved.json()["points"] if point["logical_role"]}
    assert by_role["space_temp"]["display_label"] is None
    assert by_role["supply_air_temp"]["display_label"] == "Sales Floor Supply Air"
    too_long = client.put(
        f"/api/ui/devices/{device['id']}/configuration",
        headers=headers,
        json={
            "group_name": "HVAC",
            "template_key": "rtu",
            "role_points": {"space_temp": room["id"], "supply_air_temp": supply["id"]},
            "role_display_labels": {"space_temp": "x" * 121, "supply_air_temp": "Changed"},
        },
    )
    assert too_long.status_code == 422

    tree = client.get(f"/api/ui/gateways/{gateway_id}/tree", headers=headers).json()
    persisted_by_role = {point["logical_role"]: point for point in tree["points"] if point["logical_role"]}
    assert persisted_by_role["space_temp"]["display_label"] is None
    assert persisted_by_role["supply_air_temp"]["display_label"] == "Sales Floor Supply Air"
    registry = client.get("/api/ui/equipment-templates", headers=headers).json()
    assert registry["rtu"]["role_labels"]["space_temp"] == "Space Temperature"
    all_points_identifiers = {
        f"{point['object_type']}:{point['object_instance']}"
        for point in tree["points"]
        if point["saved_device_id"] == device["id"]
    }
    configure_selectable_identifiers = {
        f"{point['object_type']}:{point['object_instance']}"
        for point in tree["points"]
        if point["saved_device_id"] == device["id"]
    }
    assert all_points_identifiers == configure_selectable_identifiers
    assert len(all_points_identifiers) == 32

    source = client.get(f"/gateways/{gateway_id}/configure-tree").text
    configure = source.split("async function initConfigureTree", 1)[1].split("function renderUsers", 1)[0]
    assert "Graphic Point Bindings" in configure
    assert "Graphic Role" in configure and "Display Label" in configure and "Device Point" in configure
    assert "template.roles.map((role)" in configure
    assert "const pointOptions = (selectedId) => points.map((point)" in configure
    assert 'select.addEventListener("change"' in configure
    assert "renderRoleControls(device, select.value)" in configure
    assert 'select[data-point-id]' not in configure
    assert "select[data-role-point]" in configure
    assert "object_type}:${point.object_instance} — ${currentValue}" in configure
    assert "Saved successfully · Bindings saved:" in configure
    assert "Display label overrides:" in configure


def test_mapping_labels_apply_to_new_device_but_retain_explicit_device_override() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    mapping = client.post(
        "/api/ui/mapping-templates",
        headers=headers,
        json={
            "name": f"SE8650 Labels {uuid4().hex[:6]}",
            "graphic_template_key": "rtu",
            "rules": [{
                "logical_role": "space_temp",
                "display_label": "Standard Room Temperature",
                "match_field": "object_name",
                "match_value": "Room Temperature",
                "object_type": "analog-value",
                "required": False,
            }],
        },
    ).json()

    devices = []
    for instance in (1, 2):
        device = client.post(
            f"/api/ui/gateways/{gateway_id}/devices",
            headers=headers,
            json={"device_instance": 8600 + instance, "device_name": f"RTU-{instance}"},
        ).json()
        point = client.post(
            f"/api/ui/devices/{device['id']}/points",
            headers=headers,
            json={"object_type": "analog-value", "object_instance": 100, "object_name": "Room Temperature", "present_value": str(72 + instance)},
        ).json()
        label = "Sales Floor Temperature" if instance == 2 else None
        client.put(
            f"/api/ui/devices/{device['id']}/configuration",
            headers=headers,
            json={
                "group_name": "HVAC",
                "template_key": "rtu",
                "mapping_template_id": mapping["id"],
                "role_points": {"space_temp": point["id"]} if label else {},
                "role_display_labels": {"space_temp": label} if label else {},
            },
        )
        result = client.post(f"/api/ui/devices/{device['id']}/mapping-template/{mapping['id']}/apply", headers=headers)
        assert result.status_code == 200
        devices.append(device)

    tree = client.get(f"/api/ui/gateways/{gateway_id}/tree", headers=headers).json()
    labels = {
        point["saved_device_id"]: (point["logical_role"], point["display_label"], point["present_value"])
        for point in tree["points"]
    }
    assert labels[devices[0]["id"]] == ("space_temp", "Standard Room Temperature", "73")
    assert labels[devices[1]["id"]] == ("space_temp", "Sales Floor Temperature", "74")
    workspace = client.get(f"/gateways/{gateway_id}").text
    detail = client.get(f"/gateways/{gateway_id}/devices/{devices[1]['id']}").text
    assert "resolvedRoleLabel(points.get(role), template, role)" in workspace
    assert "resolvedRoleLabel(display.point, template, role)" in detail


def test_new_csv_display_label_round_trip() -> None:
    email = f"phase2-admin-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="admin", status="active")
    headers = user_headers(email, user_id)
    csv_text = (
        "template_name,graphic_template,logical_role,display_label,match_field,match_value,object_type,required\r\n"
        "Labeled RTU,rtu,space_temp,Sales Floor Temperature,object_name,Room Temperature,analog-value,false\r\n"
    )
    imported = client.post("/api/ui/mapping-templates/import", headers=headers, json=csv_text)
    assert imported.status_code == 200, imported.text
    assert imported.json()["rules"][0]["display_label"] == "Sales Floor Temperature"
    exported = client.get(f"/api/ui/mapping-templates/{imported.json()['id']}/export", headers=headers)
    row = next(csv.DictReader(io.StringIO(exported.text)))
    assert list(row) == CSV_COLUMNS
    assert row["display_label"] == "Sales Floor Temperature"


def test_configuration_duplicate_role_names_points_and_rolls_back_then_succeeds() -> None:
    gateway_id = f"GW-{uuid4().hex[:10]}"
    create_gateway_token(gateway_id)
    email = f"phase2-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="operator", status="active")
    headers = user_headers(email, user_id)
    device = client.post(f"/api/ui/gateways/{gateway_id}/devices", headers=headers, json={"device_instance": 1101}).json()
    room = client.post(f"/api/ui/devices/{device['id']}/points", headers=headers, json={"object_type":"analog-value","object_instance":1,"object_name":"Room Temperature","present_value":"72.1"}).json()
    av25 = client.post(f"/api/ui/devices/{device['id']}/points", headers=headers, json={"object_type":"analog-value","object_instance":25,"object_name":"AV25","present_value":"73.0"}).json()
    duplicate_payload = {"group_name":"HVAC","template_key":"rtu","point_roles":{room["id"]:"space_temp",av25["id"]:"space_temp"}}
    conflict = client.put(f"/api/ui/devices/{device['id']}/configuration", headers=headers, json=duplicate_payload)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "space_temp is assigned to Room Temperature and AV25"
    tree = client.get(f"/api/ui/gateways/{gateway_id}/tree", headers=headers).json()
    assert not any(group["name"] == "HVAC" for group in tree["groups"])
    assert all(point["logical_role"] is None for point in tree["points"])
    duplicate_payload["point_roles"][av25["id"]] = "supply_air_temp"
    saved = client.put(f"/api/ui/devices/{device['id']}/configuration", headers=headers, json=duplicate_payload)
    assert saved.status_code == 200
    assert {point["logical_role"] for point in saved.json()["points"]} == {"space_temp", "supply_air_temp"}
    created = client.post(f"/api/ui/devices/{device['id']}/mapping-template", headers=headers, json={"name":"SE8650 generated"})
    assert created.status_code == 200
    rules = created.json()["rules"]
    assert {(rule["logical_role"], rule["match_value"], rule["object_type"]) for rule in rules} == {("space_temp","Room Temperature","analog-value"),("supply_air_temp","AV25","analog-value")}
    assert all(set(rule) == {"logical_role","display_label","match_field","match_value","object_type","required"} for rule in rules)
    assert {rule["display_label"] for rule in rules} == {"Space Temperature", "Supply Air Temperature"}


def test_mapping_csv_download_uses_authenticated_blob_transport_and_real_crlf_example() -> None:
    page = client.get("/gateways/GW001/configure-tree")
    assert page.status_code == 200
    source = page.text
    assert "async function authenticatedResponse" in source
    assert '"Authorization": `Bearer ${session.access_token}`' in source
    assert 'new Blob([text], {type:"text/csv;charset=utf-8"})' in source
    assert "URL.createObjectURL" in source
    assert "URL.revokeObjectURL" in source
    assert "downloadMappingTemplateCsv(templateId" in source
    assert 'data-mapping-template-id="${escapeHtml(item.id)}"' in source
    assert '<a class="button secondary" href="/api/ui/mapping-templates/' not in source
    assert "window.location.assign(`/api/ui/mapping-templates/" not in source
    assert '.join("\\r\\n")' in source
    assert "required\\\\nSE8650 RTU" not in source
    assert 'downloadCsvText(example, "se8650-rtu-example.csv")' in source


def test_legacy_seven_column_csv_imports_and_exports_as_eight_human_readable_columns() -> None:
    email = f"phase2-admin-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="admin", status="active")
    headers = user_headers(email, user_id)
    rows = [
        {
            "template_name": "SE8650 RTU",
            "graphic_template": "rtu",
            "logical_role": logical_role,
            "match_field": "object_name",
            "match_value": match_value,
            "object_type": object_type,
            "required": "false",
        }
        for logical_role, match_value, object_type in EXAMPLE_RULES
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=LEGACY_CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    example_csv = stream.getvalue()

    assert example_csv.count("\r\n") == len(rows) + 1
    parsed_example = list(csv.DictReader(io.StringIO(example_csv)))
    assert list(parsed_example[0]) == LEGACY_CSV_COLUMNS
    assert len(parsed_example) == 7
    assert all(len(row) == 7 for row in parsed_example)

    imported = client.post("/api/ui/mapping-templates/import", headers=headers, json=example_csv)
    assert imported.status_code == 200, imported.text
    assert imported.json()["name"] == "SE8650 RTU"
    assert len(imported.json()["rules"]) == 7
    assert all(rule["display_label"] is None for rule in imported.json()["rules"])

    assert client.get(f"/api/ui/mapping-templates/{imported.json()['id']}/export").status_code == 401
    exported = client.get(f"/api/ui/mapping-templates/{imported.json()['id']}/export", headers=headers)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert b"\r\n" in exported.content
    parsed_export = list(csv.DictReader(io.StringIO(exported.text)))
    assert list(parsed_export[0]) == CSV_COLUMNS
    assert len(parsed_export) == 7
    assert all(len(row) == 8 for row in parsed_export)
    assert all(row["display_label"] for row in parsed_export)
    assert next(row for row in parsed_export if row["logical_role"] == "space_temp")["display_label"] == "Space Temperature"
    assert {
        (row["logical_role"], row["match_value"], row["object_type"], row["required"])
        for row in parsed_export
    } == {(role, match_value, object_type, "false") for role, match_value, object_type in EXAMPLE_RULES}


def test_mapping_csv_export_uses_standard_escaping_for_commas_quotes_and_newlines() -> None:
    email = f"phase2-admin-{uuid4().hex[:8]}@example.com"
    user_id = create_operator_user(email, role="admin", status="active")
    headers = user_headers(email, user_id)
    match_value = 'Room, "North"\nTemperature'
    created = client.post(
        "/api/ui/mapping-templates",
        headers=headers,
        json={
            "name": 'Quoted, "Template"',
            "graphic_template_key": "rtu",
            "rules": [{"logical_role": "space_temp", "match_field": "object_name", "match_value": match_value, "object_type": "analog-value", "required": False}],
        },
    )
    assert created.status_code == 200, created.text
    exported = client.get(f"/api/ui/mapping-templates/{created.json()['id']}/export", headers=headers)
    assert exported.status_code == 200
    assert '"Quoted, ""Template"""' in exported.text
    assert '"Room, ""North""\nTemperature"' in exported.text
    parsed = list(csv.DictReader(io.StringIO(exported.text)))
    assert len(parsed) == 1
    assert parsed[0]["template_name"] == 'Quoted, "Template"'
    assert parsed[0]["match_value"] == match_value


def test_workspace_renderer_emits_direct_weather_and_device_tiles_with_structured_values() -> None:
    page = client.get("/gateways/GW001")
    assert page.status_code == 200
    renderer = page.text.split("function renderSiteEquipmentOverview", 1)[1].split("function resourcePercent", 1)[0]
    assert 'class="site-equipment-section"' in renderer
    assert 'class="equipment-grid"' in renderer
    assert renderer.count('<article class="tile weather-card weather-summary-card">') == 1
    assert '<div class="weather-main">${bmsIcon("cloud", "weather-icon")}' in renderer
    assert renderer.count('<article class="tile equipment-summary-card">') == 1
    assert '<div class="equipment-card-icon">${bmsIcon("building")}</div>' in renderer
    assert '<div class="equipment-grid">${weatherCard}${classified.map(tile).join("")' in renderer
    assert 'class="equipment-key"' in renderer
    assert 'class="equipment-reading"' in renderer
    assert 'class="secondary equipment-action"' in renderer
    assert 'data-device-href="${href}"' in renderer
    assert '<section class="equipment-category-section">' not in renderer
    assert '<article class="equipment-summary-card">' not in renderer
    assert "byCategory" not in renderer


def test_workspace_css_constrains_summary_grid_cards_and_inline_svg_icons() -> None:
    page = client.get("/gateways/GW001")
    assert page.status_code == 200
    workspace_shell = '<section class="bms-shell" aria-labelledby="bms-graphic-title">'
    workspace_style = '<style>\n        .bms-shell {\n          --bg-surface:#121317'
    assert page.text.index(workspace_style) < page.text.index(workspace_shell)
    workspace_css = page.text.split(
        workspace_style, 1
    )[1].split("</style>", 1)[0]

    assert "{{" not in workspace_css
    assert "}}" not in workspace_css
    assert (
        ".equipment-grid { display:grid; "
        "grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr)); "
        "gap:12px; margin-top:18px; min-width:0; align-items:start; }"
    ) in workspace_css
    assert (
        ".bms-inline-icon { display:block; width:16px; height:16px; "
        "max-width:16px; max-height:16px;"
    ) in workspace_css
    assert (
        ".equipment-card-icon { display:flex; width:36px; height:36px; "
        "min-width:36px; min-height:36px; max-width:36px; max-height:36px;"
    ) in workspace_css
    assert (
        ".equipment-card-icon .bms-inline-icon { display:block; width:18px; "
        "height:18px; max-width:18px; max-height:18px;"
    ) in workspace_css
    assert (
        ".weather-summary-card .weather-icon,.weather-main .weather-icon { "
        "display:block; width:40px; height:40px; max-width:40px; max-height:40px;"
    ) in workspace_css
    assert (
        ".equipment-summary-card,.weather-summary-card { height:auto; min-height:0; "
        "align-self:start; break-inside:avoid; }"
    ) in workspace_css
    assert "@media print {" in workspace_css
    assert "page-break-inside:avoid;" in workspace_css


def test_device_renderer_emits_reference_tile_hierarchy_and_no_legacy_graphic_markup() -> None:
    page = client.get("/gateways/GW001/devices/device-1")
    assert page.status_code == 200
    renderer = page.text.split("async function loadDeviceGraphic", 1)[1].split("async function initConfigureTree", 1)[0]
    assert '<div class="topbar equipment-topbar">' in renderer
    assert 'class="brand-mark"' in renderer
    assert 'class="status-pill ${deviceState}"' in renderer
    assert '<article class="tile c-info">' in renderer
    assert '<article class="tile c-weather weather-card">' in renderer
    assert '<article class="tile c-alarm">' in renderer
    assert '<article class="tile c-temp">' in renderer
    assert '<article class="tile c-status6 eq-tile ${state}">' in renderer
    assert '<article class="tile c-setpoint">' in renderer
    assert '<article class="tile c-trend">' in renderer
    assert 'class="stat-value"' in renderer
    assert 'class="eq-name"' in renderer and 'class="eq-state"' in renderer
    assert "Trend Log — ${escapeHtml(resolvedRoleLabel" in renderer
    assert "No trend series connected to this graphic." in renderer
    assert '<div class="rtu-topbar">' not in renderer
    assert "Supply Fan Statusactive" not in renderer
    assert renderer.count("target.innerHTML =") == 2  # graphic and error fallback
