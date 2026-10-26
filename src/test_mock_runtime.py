from unittest.mock import Mock, call

import pandas as pd
import pytest
from fastapi.testclient import TestClient
import dashboard
import db
import session_bars as sb
from mock_config import MockConfig
from mock_runtime import MockRuntime


@pytest.fixture
def replay(tmp_path, monkeypatch):
    runtime = MockRuntime(tmp_path / "mock.db", config=MockConfig())
    monkeypatch.setattr(dashboard.app.state, "mock_runtime", runtime, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, "db_path", runtime.db_path)
    monkeypatch.setitem(dashboard.CONFIG, "code", runtime.code)
    dashboard._LOAD_CACHE.clear()
    client = TestClient(dashboard.app)
    yield runtime, client
    client.close()
    runtime.close()
    dashboard._LOAD_CACHE.clear()


@pytest.fixture
def empty_replay(tmp_path, monkeypatch):
    runtime = MockRuntime(tmp_path / "empty.db", config=MockConfig.model_validate({"replay": {"start_bar": 0}}))
    monkeypatch.setattr(dashboard.app.state, "mock_runtime", runtime, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, "db_path", runtime.db_path)
    monkeypatch.setitem(dashboard.CONFIG, "code", runtime.code)
    dashboard._LOAD_CACHE.clear()
    with TestClient(dashboard.app) as client:
        yield runtime, client
    dashboard._LOAD_CACHE.clear()


def test_zero_start_has_no_bars_or_future_quote_and_empty_charts_work(empty_replay):
    runtime, client = empty_replay
    status = client.get("/api/mock").json()
    assert status["cursor"] == 0 and status["total"] == status["remaining"] == 4200
    assert not status["playing"] and status["price"] is None
    assert runtime.now() == pd.Timestamp("2026-10-05 08:45")
    with db.session(runtime.db_path) as conn:
        assert db.load_minute_bars(conn, runtime.code).empty
    trading = client.get("/api/trading/status").json()
    assert trading["connected"] and not trading["quote_connected"]
    assert trading["last_quote"] is None and trading["last_quote_at"] is None
    state = client.get("/api/state").json()
    assert not state["ready"] and "第一根" in state["message"]
    assert client.get("/api/trading/contracts").status_code == 200
    assert client.get("/api/trading/margin").status_code == 200
    for timeframe in ("1m", "5m", "15m", "1h"):
        response = client.get(f"/api/chart?timeframe={timeframe}")
        assert response.status_code == 200, response.text
        chart = response.json()
        assert chart["timeframe"] == timeframe
        assert chart["bars"] == chart["boxes"] == chart["series"] == chart["trendlines"] == []
    assert client.get("/api/bars").json()["bars"] == []
    assert client.get("/api/boxes").json()["boxes"] == []
    assert client.get("/api/series").json()["series"] == []
    # An unrelated code must not silently masquerade as this empty replay.
    assert client.get("/api/chart?code=UNKNOWN").status_code == 404
    order = client.post("/api/trading/orders", json={
        "contract_code": "TMF209912", "action": "Buy", "quantity": 1, "price": 0,
        "price_type": "MKT", "order_type": "IOC", "simulation": True, "confirmed": True,
    })
    assert order.status_code == 503 and "尚無可用行情" in order.json()["detail"]
    assert not runtime.broker.trades


def test_zero_start_first_step_and_reset_never_publish_last_scenario_bar(empty_replay):
    runtime, client = empty_replay
    result = client.post("/api/mock/control", json={"action": "step", "steps": 1})
    assert result.status_code == 200
    assert result.json()["cursor"] == 1 and result.json()["price"] == 27000
    assert runtime.now() == pd.Timestamp("2026-10-05 08:46")
    with db.session(runtime.db_path) as conn:
        bars = db.load_minute_bars(conn, runtime.code)
    assert len(bars) == 1 and bars.iloc[0]["close"] == 27000
    assert client.get("/api/trading/status").json()["quote_connected"]
    assert client.get("/api/state").json()["hourly_atr"] is None
    for timeframe in ("1m", "5m", "15m", "1h"):
        chart = client.get(f"/api/chart?timeframe={timeframe}").json()
        assert len(chart["bars"]) == 1 and chart["bars"][0]["close"] == 27000
    runtime.advance(5)
    result = client.post("/api/mock/control", json={"action": "reset", "confirmed": True}).json()
    assert result["cursor"] == 0 and result["price"] is None and not result["playing"]
    assert runtime.now() == pd.Timestamp("2026-10-05 08:45")
    assert client.get("/api/chart").json()["bars"] == []
    assert client.get("/api/trading/status").json()["last_quote"] is None
    assert not client.get("/api/state").json()["ready"]


def manual_order(client, action="Buy"):
    result = client.post("/api/trading/orders", json={
        "contract_code": "TMF209912", "action": action, "quantity": 1, "price": 0,
        "price_type": "MKT", "order_type": "IOC", "simulation": True, "confirmed": True,
    })
    assert result.status_code == 200, result.text
    return result.json()


def save_curve(runtime, client, price, slope=0):
    hourly = sb.minute_bars_to_hourly(runtime.bars.iloc[:runtime.cursor])
    last = runtime.bars.iloc[runtime.cursor - 1].minute_start
    current_index = len(hourly) - 1 + (last - hourly.iloc[-1].hour_start).total_seconds() / 3600
    anchors = [{"time": int(hourly.iloc[index].hour_start.timestamp()), "price": price - slope * (current_index - index)}
               for index in (len(hourly) - 5, len(hourly) - 3, len(hourly) - 1)]
    result = client.post("/api/trendlines", json={"kind": "curve", "anchors": anchors})
    assert result.status_code == 200, result.text
    return result.json()["id"]


def arm(client, curve_id):
    result = client.post("/api/mock/curve-entry", json={"curve_id": curve_id, "contract_code": "TMF209912", "quantity": 1, "confirmed": True})
    assert result.status_code == 200, result.text
    return result.json()


def test_mock_ignores_production_mode_and_does_not_expose_future_bars(replay, monkeypatch):
    runtime, client = replay
    monkeypatch.setenv("SHIOAJI_SIMULATION", "0")
    status = client.get("/api/trading/status").json()
    assert status["mode"] == "離線 Mock API" and status["simulation"] is True
    assert status["connected"] and status["quote_connected"]
    assert runtime.service._thread is None
    with db.session(runtime.db_path) as conn:
        bars = db.load_minute_bars(conn, runtime.code)
    assert len(bars) == runtime.config.warmup_minutes
    assert bars.minute_start.max() < runtime.now()
    assert client.get("/api/state").json()["hourly_atr"] > 0
    assert client.get("/api/chart").status_code == 200


@pytest.mark.parametrize("action,direction", [("Buy", 1), ("Sell", -1)])
def test_1_manual_fill_shows_two_atr_stop_and_target(replay, action, direction):
    runtime, client = replay
    order = manual_order(client, action)
    assert order["entry_status"] == "Filled" and order["filled_quantity"] == 1
    assert order["avg_fill_price"] == runtime.broker.price
    assert order["stop_price"] == pytest.approx(order["avg_fill_price"] - direction * 2 * order["atr"])
    assert order["target_price"] == pytest.approx(order["avg_fill_price"] + direction * 2 * order["atr"])
    assert len(runtime.broker.trades) == 1


@pytest.mark.parametrize("action,reason", [("Buy", "target"), ("Sell", "stop")])
def test_fixed_exits_fill_and_flatten_position(replay, action, reason):
    runtime, client = replay
    runtime.advance(60)
    order = manual_order(client, action)
    runtime.advance(90)
    row = runtime.service._active_order(order["id"])
    assert row["trigger_reason"] == reason
    assert row["exit_status"] == "Filled" and row["active"] == 0
    assert runtime.broker.position == 0
    assert len(runtime.broker.trades) == 2


def test_2_manually_saved_curve_replaces_target_and_triggers_trailing_exit(replay):
    runtime, client = replay
    runtime.advance(100)
    order = manual_order(client)
    # A rising hand-drawn curve starts 5 points below price, then overtakes it.
    curve_id = save_curve(runtime, client, runtime.broker.price - 5, slope=180)
    response = client.put(f"/api/trading/orders/{order['id']}/curve-selection", json={"curve_id": curve_id})
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["stop_price"] == order["stop_price"]
    assert changed["target_price"] is None and changed["curve_id"] == curve_id
    runtime.advance(20)
    row = runtime.service._active_order(order["id"])
    assert row["trigger_reason"] == "curve"
    assert row["exit_status"] == "Filled" and not row["active"]
    assert runtime.broker.position == 0


def test_3_curve_breakout_buys_once_and_creates_two_atr_levels(replay):
    runtime, client = replay
    curve_id = save_curve(runtime, client, runtime.broker.price - 20)
    arm(client, curve_id)
    assert not runtime.broker.trades  # No historical crossing or immediate buy on arming.
    runtime.advance(90)
    entry = client.get("/api/mock").json()["entry"]
    assert entry["state"] == "triggered"
    row = runtime.service._active_order(entry["order_id"])
    assert row["action"] == "Buy" and row["filled_quantity"] == 1
    assert row["curve_id"] is None  # Entry curve does not implicitly become an exit curve.
    assert row["stop_price"] == pytest.approx(row["avg_fill_price"] - 2 * row["atr"])
    assert row["target_price"] == pytest.approx(row["avg_fill_price"] + 2 * row["atr"])
    runtime.advance(5)
    assert len(runtime.broker.trades) == 1


def test_uncertain_breakout_submission_is_not_retried(replay, monkeypatch):
    runtime, client = replay
    arm(client, save_curve(runtime, client, runtime.broker.price - 20))
    submit = Mock(side_effect=TimeoutError("uncertain"))
    monkeypatch.setattr(runtime.service, "place_order", submit)
    runtime.advance(100)
    assert runtime.entry["state"] == "failed"
    assert submit.call_count == 1


def test_deleted_or_cancelled_entry_curve_never_submits(replay):
    runtime, client = replay
    curve_id = save_curve(runtime, client, runtime.broker.price - 20)
    arm(client, curve_id)
    client.delete(f"/api/trendlines/{curve_id}")
    runtime.advance()
    assert runtime.entry["state"] == "failed" and not runtime.broker.trades
    curve_id = save_curve(runtime, client, runtime.broker.price - 20)
    arm(client, curve_id)
    client.delete("/api/mock/curve-entry")
    runtime.advance(90)
    assert runtime.entry["state"] == "cancelled" and not runtime.broker.trades


def test_controls_confirmation_and_reset_are_isolated(replay):
    runtime, client = replay
    assert client.post("/api/mock/control", json={"action": "step", "steps": 0}).status_code == 422
    assert client.post("/api/mock/control", json={"action": "reset"}).status_code == 422
    assert client.post("/api/mock/control", json={"action": "play"}).json()["playing"]
    assert not client.post("/api/mock/control", json={"action": "pause"}).json()["playing"]
    assert client.post("/api/mock/control", json={"action": "step", "steps": 1}).json()["cursor"] == runtime.config.warmup_minutes + 1
    manual_order(client)
    result = client.post("/api/mock/control", json={"action": "reset", "confirmed": True}).json()
    assert result["cursor"] == runtime.config.warmup_minutes and result["entry"] is None
    assert client.get("/api/trading/orders").json() == []
    assert client.get("/api/trendlines").json() == []


def test_mock_endpoints_cannot_arm_a_real_broker(monkeypatch):
    monkeypatch.setattr(dashboard.app.state, "mock_runtime", None, raising=False)
    client = TestClient(dashboard.app)
    assert client.get("/api/mock").json() == {"enabled": False}
    assert client.post("/api/mock/control", json={"action": "play"}).status_code == 404
    assert client.post("/api/mock/curve-entry", json={"curve_id": 1, "contract_code": "TMF209912", "confirmed": True}).status_code == 404


def test_runtime_rejects_existing_database(tmp_path):
    path = tmp_path / "existing.db"
    path.write_text("do not overwrite")
    with pytest.raises(ValueError, match="獨立資料庫"):
        MockRuntime(path)
    assert path.read_text() == "do not overwrite"


def test_rebound_then_pullback_closes_long_at_a_profit_using_curve(tmp_path, monkeypatch):
    config = MockConfig.model_validate({"replay": {
        "start_at": "before_pullback", "minutes_before_pullback": 60,
    }})
    runtime = MockRuntime(tmp_path / "pullback.db", config=config)
    monkeypatch.setattr(dashboard.app.state, "mock_runtime", runtime, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, "db_path", runtime.db_path)
    monkeypatch.setitem(dashboard.CONFIG, "code", runtime.code)
    dashboard._LOAD_CACHE.clear()
    try:
        with TestClient(dashboard.app) as client:
            assert runtime.cursor == 4020 and not runtime.playing
            assert runtime.snapshot()["phase"] == "V 型反彈"
            with db.session(runtime.db_path) as conn:
                assert len(db.load_minute_bars(conn, runtime.code)) == 4020
            initial_price = runtime.broker.price
            assert initial_price == pytest.approx(24958, abs=1)
            order = manual_order(client)
            curve_id = save_curve(runtime, client, price=25040)
            changed = client.put(f"/api/trading/orders/{order['id']}/curve-selection", json={"curve_id": curve_id})
            assert changed.status_code == 200, changed.text
            assert changed.json()["target_price"] is None
            assert changed.json()["stop_price"] == order["stop_price"]
            runtime.advance(60)
            at_peak = runtime.service._active_order(order["id"])
            assert at_peak["active"] and at_peak["trigger_reason"] is None
            assert runtime.broker.price > 25040
            runtime.advance(30)
            closed = runtime.service._active_order(order["id"])
            assert runtime.snapshot()["phase"] == "反彈後回檔"
            assert closed["trigger_reason"] == "curve"
            assert closed["exit_status"] == "Filled" and not closed["active"]
            assert runtime.broker.trades[-1].status.average_price > initial_price
            assert runtime.broker.position == 0 and len(runtime.broker.trades) == 2
            runtime.reset()
            assert runtime.cursor == 4020 and not runtime.playing
    finally:
        runtime.close()
        dashboard._LOAD_CACHE.clear()


def test_config_controls_replay_clock_phases_account_and_reset(tmp_path, monkeypatch):
    import json
    import mock_config

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "market": {"decline_minutes": 1800, "rebound_minutes": 30, "pullback_minutes": 10},
        "replay": {"minutes_before_bottom": 2, "interval_seconds": 0.25, "browser_poll_seconds": 0.5},
        "broker": {"equity": 500000, "initial_margin_per_contract": 10000,
                   "maintenance_margin_per_contract": 8000},
    }))
    monkeypatch.setattr(mock_config, "DEFAULT_CONFIG_PATH", path)
    runtime = MockRuntime(tmp_path / "custom.db")
    try:
        assert runtime.cursor == 1798
        with db.session(runtime.db_path) as conn:
            assert len(db.load_minute_bars(conn, runtime.code)) == 1798
        assert runtime.snapshot()["replay"]["browser_poll_seconds"] == 0.5
        assert runtime.broker.margin(None).equity == 500000
        initial_time = runtime.now()
        runtime.playing = True
        stop = Mock()
        stop.wait.side_effect = [False, True]
        monkeypatch.setattr(runtime, "_stop", stop)
        runtime._loop()
        assert stop.wait.call_args_list == [call(0.25), call(0.25)]
        assert runtime.now() == initial_time + pd.Timedelta(minutes=1)
        runtime.advance(2)
        assert runtime.snapshot()["phase"] == "V 型反彈"
        runtime.advance(30)
        assert runtime.snapshot()["phase"] == "反彈後回檔"
        runtime.reset()
        assert runtime.cursor == 1798 and not runtime.playing
        assert runtime.now() == initial_time
        assert runtime.broker.settings == runtime.config.broker
    finally:
        runtime.close()
