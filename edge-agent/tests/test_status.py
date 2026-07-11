from pathlib import Path

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.status import collect_status


def test_collect_status_includes_lightweight_resource_metrics(tmp_path: Path) -> None:
    config = AgentConfig(gateway_id="GW001", site_id="site-1", cloud_url="https://cloud.example", sqlite_path=tmp_path / "agent.db")

    status = collect_status(config, sqlite_db_ok=False)

    assert status["cpu_count"] >= 1
    assert status["disk_free_mb"] is not None
    assert "cpu_load_1m" in status
    assert "memory_used_pct" in status
