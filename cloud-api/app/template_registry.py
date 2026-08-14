"""Cloud-owned equipment templates and their permitted logical point roles."""

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
