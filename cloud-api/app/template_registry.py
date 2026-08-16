"""Cloud-owned equipment templates and their permitted logical point roles."""

ROLE_LABELS = {
    "space_temp": "Space Temperature",
    "supply_air_temp": "Supply Air Temperature",
    "return_air_temp": "Return Air Temperature",
    "outside_air_temp": "Outside Air Temperature",
    "supply_fan_status": "Supply Fan",
    "supply_fan_speed": "Supply Fan Speed",
    "cool_stage_1": "Cool Stage 1",
    "cool_stage_2": "Cool Stage 2",
    "heat_stage_1": "Heat Stage 1",
    "heat_stage_2": "Heat Stage 2",
    "reversing_valve": "Reversing Valve",
    "oa_damper_position": "OA Damper",
    "filter_status": "Filter Status",
    "occupied_cool_sp": "Occupied Cool Setpoint",
    "occupied_heat_sp": "Occupied Heat Setpoint",
    "unoccupied_cool_sp": "Unoccupied Cool Setpoint",
    "unoccupied_heat_sp": "Unoccupied Heat Setpoint",
    "effective_cool_sp": "Effective Cool Setpoint",
    "effective_heat_sp": "Effective Heat Setpoint",
    "occupancy_mode": "Occupancy Mode",
    "operating_need": "Operating Need",
    "alarm_count": "Alarm Count",
}

TEMPLATES = {
    "rtu": {
        "label": "RTU",
        "categories": {"HVAC"},
        "roles": ("space_temp", "supply_air_temp", "return_air_temp", "outside_air_temp", "supply_fan_status", "supply_fan_speed", "cool_stage_1", "cool_stage_2", "heat_stage_1", "heat_stage_2", "reversing_valve", "oa_damper_position", "filter_status", "occupied_cool_sp", "occupied_heat_sp", "unoccupied_cool_sp", "unoccupied_heat_sp", "effective_cool_sp", "effective_heat_sp", "occupancy_mode", "operating_need", "alarm_count"),
        "summary_roles": ("space_temp", "supply_air_temp", "operating_need", "occupancy_mode"),
    },
    "ahu": {
        "label": "AHU",
        "categories": {"HVAC"},
        "roles": ("space_temp", "supply_air_temp", "return_air_temp", "outside_air_temp", "supply_fan_status", "supply_fan_speed", "cool_stage_1", "heat_stage_1", "oa_damper_position", "filter_status", "occupied_cool_sp", "occupied_heat_sp", "effective_cool_sp", "effective_heat_sp", "occupancy_mode", "operating_need", "alarm_count"),
        "summary_roles": ("space_temp", "supply_air_temp", "operating_need", "occupancy_mode"),
    },
    "minisplit": {
        "label": "Minisplit",
        "categories": {"HVAC"},
        "roles": ("space_temp", "supply_air_temp", "operating_need", "occupancy_mode", "effective_cool_sp", "effective_heat_sp", "supply_fan_status", "alarm_count"),
        "summary_roles": ("space_temp", "supply_air_temp", "operating_need", "occupancy_mode"),
    },
}

def template_for(key: str | None) -> dict | None:
    return TEMPLATES.get(key or "")


def default_display_label(role: str) -> str:
    """Resolve the current customer-facing registry default for a logical role."""
    return ROLE_LABELS.get(role, role.replace("_", " ").title())
