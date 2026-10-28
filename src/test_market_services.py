import plistlib
import pandas as pd
import db
import market_health as mh
from market_services import launchd_specs, preflight
from test_monitor_recovery import frame
from test_market_health import calendar_document
import json


def test_jobs_have_separate_ownership_and_never_auto_restart_trading(tmp_path):
    specs = launchd_specs(tmp_path, "/opt/conda/envs/tss/bin/python", tmp_path / "tmf.db", code="TMFR1",
                           mode="production", health_config=tmp_path / "health.json", notify=True)
    assert len(specs) == 3
    for label, document in specs.items():
        roundtrip = plistlib.loads(plistlib.dumps(document))
        assert roundtrip == document
        assert document["EnvironmentVariables"]["SHIOAJI_SIMULATION"] == "0"
        assert document["KeepAlive"] == ("trading" not in label)
        assert all(isinstance(value, str) for value in document["ProgramArguments"])
        if "watchdog" in label:
            assert "--notify" in document["ProgramArguments"]


def test_preflight_is_read_only_and_does_not_claim_broker_verification(tmp_path):
    path, calendar_path = tmp_path / "test.db", tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(calendar_document()))
    with db.session(path) as conn:
        db.upsert_minute_bars(conn, "TMFR1", frame("2026-10-27 19:03", "2026-10-27 19:06"))
    result = preflight(path, "TMFR1", "production", mh.HealthSettings(calendar_path=str(calendar_path)),
                       now=pd.Timestamp("2026-10-27 19:07"))
    assert result["configuration_ready"] and result["database_readable"]
    assert not result["runtime_verified"] and result["unresolved_orders"] == 0
    assert result["runtime"]["monitor"]["pid"] is None
    assert not mh.health_path(path, "monitor", "TMFR1").exists()
