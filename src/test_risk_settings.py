"""Persistent risk changes, fresh-tick protection and HTTP conflict semantics."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard
import db
import trading
import session_bars as sb
import trendline as tl
from test_trading import fake_service  # shared offline SDK fixture


def filled(service, api, action="Buy", quantity=1, price=46270):
    service._now = lambda: pd.Timestamp("2026-10-05 11:00:00")
    order = service.place_order({"confirmed": True, "simulation": True, "contract_code": "TMF202611",
                                 "action": action, "quantity": quantity, "price": price,
                                 "price_type": "LMT", "order_type": "ROD"})
    entry = api.trades[-1]
    entry.status.status = "Filled"
    entry.status.deal_quantity = quantity
    entry.status.deals = [SimpleNamespace(quantity=quantity, price=price)]
    service._persist_trade_state(order["id"], entry=entry)
    return service._public_order(service._active_order(order["id"]))


def tick(service, price, stamp="2026-10-05 11:00:00"):
    service._on_tick(SimpleNamespace(code="TMFX6", close=price, datetime=pd.Timestamp(stamp)))


def save(service, order, **changes):
    payload = {key: order[key] for key in ("curve_id", "stop_price", "target_price", "risk_revision")}
    payload["breakeven_enabled"] = bool(order["breakeven_enabled"])
    payload.update(changes)
    return service.update_risk_settings(order["id"], **payload)


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h"])
def test_all_curve_timeframes_can_replace_and_adjust_profit_target(fake_service, timeframe):
    service, api, _ = fake_service
    order = filled(service, api)
    minutes = pd.DataFrame([dict(minute_start=stamp, open=46270, high=46270, low=46270,
                                close=46270, volume=1, amount=46270)
                           for stamp in pd.date_range('2026-10-05 08:45', '2026-10-05 10:59', freq='min')])
    minutes = sb.add_sessions(minutes)
    source = tl.source_bars(minutes, timeframe)
    stamps = source['hour_start' if timeframe == '1h' else 'timestamp'].iloc[[0, 1, 2]]
    anchors = [{'time': int(stamp.value // 10**9), 'price': price}
               for stamp, price in zip(stamps, (46240, 46290, 46270))]
    with db.session(service.db_path) as conn:
        db.upsert_minute_bars(conn, service.code, minutes)
        curve_id = db.insert_trendline(conn, service.code, anchors, kind="curve", timeframe=timeframe)
    changed = save(service, order, curve_id=curve_id)
    expected = tl.values_at_times(source, anchors, [minutes.iloc[-1]['minute_start']], timeframe=timeframe).iloc[0]
    assert changed['curve_id'] == curve_id and changed['curve_timeframe'] == timeframe
    assert changed['target_price'] is None and changed['stop_price'] == order['stop_price']
    assert changed['curve_price'] == pytest.approx(expected)
    adjusted = service.update_curve(order['id'], expected + 20)
    assert adjusted['curve_price'] == pytest.approx(expected + 20)
    with db.session(service.db_path) as conn:
        curve = next(item for item in db.load_trendlines(conn, service.code) if item['id'] == curve_id)
    assert curve['timeframe'] == timeframe
    assert [item['time'] for item in curve['anchors']] == [item['time'] for item in anchors]
    assert [item['price'] for item in curve['anchors']] == [item['price'] + 20 for item in anchors]
    placed = service.place_order({"confirmed": True, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": 1, "price": 46270, "price_type": "LMT", "order_type": "ROD",
        "curve_id": curve_id})
    assert placed['curve_id'] == curve_id and placed['curve_timeframe'] == timeframe
    restored = save(service, adjusted, curve_id=None, target_price=47000)
    assert restored['curve_id'] is None and restored['curve_timeframe'] is None
    assert restored['target_price'] == 47000


@pytest.mark.parametrize('timeframe,interval', [('1m', 1), ('5m', 5), ('15m', 15), ('1h', 60)])
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_curve_exit_uses_source_geometry_and_adjacent_closed_minutes(fake_service, timeframe, interval, action, direction):
    service, _, _ = fake_service
    service._now = lambda: pd.Timestamp('2026-10-05 11:03:00')
    stamps = pd.date_range('2026-10-05 08:45', '2026-10-05 11:02', freq='min')
    # Beyond its end, this non-flat Bezier projects along control -> end.
    levels = [120 + (index / interval - 2) * 5 for index in (136, 137)]
    closes = [100] * (len(stamps) - 2) + [levels[0] + direction, levels[1] - direction]
    minutes = sb.add_sessions(pd.DataFrame([dict(minute_start=stamp, open=close, high=close, low=close,
        close=close, volume=1, amount=close) for stamp, close in zip(stamps, closes)]))
    anchors = [{'time': int((stamps[0] + pd.Timedelta(minutes=interval * index)).value // 10**9), 'price': price}
               for index, price in enumerate((100, 115, 120))]
    with db.session(service.db_path) as conn:
        db.upsert_minute_bars(conn, service.code, minutes)
        curve_id = db.insert_trendline(conn, service.code, anchors, kind='curve', timeframe=timeframe)
    row = dict(id='source-geometry', code=service.code, curve_id=curve_id,
               curve_check_from='2026-10-05 11:02:00', action=action)
    assert service._curve_value(row) == pytest.approx(levels[1])
    assert service._curve_close_break(row) == 'curve'
    service._curve_break_cache.clear()
    service._now = lambda: pd.Timestamp('2026-10-05 11:02:59')
    assert service._curve_close_break(row) is None  # last candle has not closed
    service._now = lambda: pd.Timestamp('2026-10-05 11:03:00')
    service._curve_break_cache.clear()
    with db.session(service.db_path) as conn:
        conn.execute("DELETE FROM minute_bars WHERE code=? AND minute_start='2026-10-05 11:01:00'", (service.code,))
        conn.commit()
    assert service._curve_close_break(row) is None  # no crossing across a missing minute


@pytest.mark.parametrize("action,direction", [("Buy", 1), ("Sell", -1)])
def test_every_tick_latches_breakeven_before_monitor_and_survives_restart(fake_service, action, direction):
    service, api, _ = fake_service
    order = filled(service, api, action, quantity=3)
    tick(service, 46270 + direction * 100)
    assert service._active_order(order["id"])["breakeven_triggered_at"] is None
    tick(service, 46270 + direction * 101)
    protected = service._active_order(order["id"])
    assert protected["stop_price"] == protected["breakeven_price"] == 46270 + direction * 5
    assert protected["risk_revision"] == order["risk_revision"] + 1
    tick(service, 46270 + direction * 200)
    tick(service, 46270 + direction * 4)
    row = service._active_order(order["id"])
    assert row["stop_price"] == protected["stop_price"]
    assert row["risk_revision"] == protected["risk_revision"]
    assert service._exit_signal(row, 46270 + direction * 4) == "stop"
    restarted = trading.TradingService(service.db_path, service.code, simulation=True, start_worker=False)
    assert restarted.orders()[0]["breakeven_triggered_at"] == protected["breakeven_triggered_at"]
    assert restarted.orders()[0]["stop_price"] == protected["stop_price"]
    restarted.close()
    with pytest.raises(trading.RiskConflict):
        save(service, order, stop_price=order["stop_price"])


@pytest.mark.parametrize("action,direction", [("Buy", 1), ("Sell", -1)])
def test_manual_lock_in_stop_is_never_loosened(fake_service, action, direction):
    service, api, _ = fake_service
    order = filled(service, api, action)
    order = save(service, order, stop_price=46270 + direction * 30)
    tick(service, 46270 + direction * 101)
    protected = service._public_order(service._active_order(order["id"]))
    assert protected["stop_price"] == 46270 + direction * 30
    with pytest.raises(ValueError, match="先關閉保本"):
        save(service, protected, stop_price=46270 + direction * 5)
    with pytest.raises(ValueError, match="先關閉保本"):
        save(service, protected, stop_price=46270 - direction * 10, breakeven_enabled=False)
    disabled = save(service, protected, breakeven_enabled=False)
    loosened = save(service, disabled, stop_price=46270 - direction * 10)
    assert not loosened["breakeven_enabled"]
    enabled = save(service, loosened, breakeven_enabled=True)
    assert enabled["stop_price"] == 46270 + direction * 5
    assert enabled["breakeven_triggered_at"]


def test_stale_out_of_order_and_invalid_quotes_do_not_arm(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    for price, stamp in [(46371, "2026-10-05 10:59:00"), (float('nan'), "2026-10-05 11:00:00")]:
        tick(service, price, stamp)
    assert not service._active_order(order["id"])["breakeven_triggered_at"]
    tick(service, 46280)
    tick(service, 46371, "2026-10-05 10:59:59")
    assert not service._active_order(order["id"])["breakeven_triggered_at"]


def test_partial_fill_average_and_sync_preserve_protection_and_custom_levels(fake_service):
    service, api, _ = fake_service
    order = filled(service, api, quantity=2)
    tick(service, 46371)
    entry = api.trades[0]
    entry.status.deal_quantity = 3
    entry.status.deals = [SimpleNamespace(quantity=3, price=46280)]
    service._persist_trade_state(order["id"], entry=entry)
    row = service._active_order(order["id"])
    assert row["stop_price"] == row["breakeven_price"] == 46285
    entry.status.deals = [SimpleNamespace(quantity=3, price=46260)]
    service._persist_trade_state(order["id"], entry=entry)
    assert service._active_order(order["id"])["stop_price"] == 46285
    row = service._public_order(service._active_order(order["id"]))
    row = save(service, row, stop_price=46300, target_price=47000)
    service._persist_trade_state(order["id"], entry=entry)
    assert service._active_order(order["id"])["target_price"] == 47000
    assert service._active_order(order["id"])["stop_price"] == 46300
    assert service._active_order(order["id"])["risk_revision"] == row["risk_revision"]


@pytest.mark.parametrize("action,direction", [("Buy", 1), ("Sell", -1)])
def test_tick_alignment_and_price_ordering(fake_service, action, direction):
    service, api, _ = fake_service
    api.Contracts.Futures.TMF[0].tick = 2.5
    order = filled(service, api, action, price=46271)
    with pytest.raises(ValueError, match="跳動"):
        save(service, order, stop_price=46001)
    with pytest.raises(ValueError, match="stop 須"):
        save(service, order, stop_price=47000 if direction > 0 else 45000)
    tick(service, 46271 + direction * 101)
    row = service._active_order(order["id"])
    assert row["stop_price"] == (46277.5 if direction > 0 else 46265)


def test_curve_stop_edit_is_not_curve_shift_and_cancel_is_atomic(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    anchors = [{"time": n, "price": 47000} for n in (1, 2, 3)]
    with db.session(service.db_path) as conn:
        curve_id = db.insert_trendline(conn, service.code, anchors, kind="curve")
    curve = save(service, order, curve_id=curve_id, stop_price=46000, target_price=49000)
    assert curve["target_price"] is None and curve["curve_price"] is None
    baseline = curve["curve_check_from"]
    updated = save(service, curve, stop_price=46100)
    assert updated["curve_check_from"] == baseline
    with db.session(service.db_path) as conn:
        assert db.load_trendlines(conn, service.code)[0]["anchors"] == anchors
    assert service._exit_signal(service._active_order(order["id"]), 46099) == "stop"
    restored = save(service, updated, curve_id=None, stop_price=46030, target_price=46510)
    assert restored["curve_id"] is None and restored["curve_check_from"] is None
    assert restored["target_price"] == 46510 and restored["stop_price"] == 46030
    with pytest.raises(ValueError, match="不存在"):
        save(service, restored, curve_id=9999)


def test_only_open_positions_and_untriggered_exits_are_editable(fake_service):
    service, api, _ = fake_service
    order = service.place_order({"confirmed": True, "simulation": True, "contract_code": "TMF202611",
                                 "action": "Buy", "quantity": 1, "price": 46270, "price_type": "LMT", "order_type": "ROD"})
    with pytest.raises(ValueError, match="已成交"):
        save(service, order)
    order = filled(service, api)
    with db.session(service.db_path) as conn:
        conn.execute("UPDATE managed_orders SET triggered_at = 'now' WHERE id = ?", (order["id"],))
        conn.commit()
    with pytest.raises(trading.RiskConflict):
        save(service, order)
    with pytest.raises(trading.RiskConflict):
        service.set_order_curve(order["id"], None)


def test_http_confirmation_conflict_and_latest_saved_exit(fake_service, monkeypatch):
    service, api, _ = fake_service
    order = filled(service, api)
    monkeypatch.setattr(dashboard, '_TRADING_SERVICE', service)
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', service.db_path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', service.code)
    tick(service, 46280)
    client = TestClient(dashboard.app)
    payload = {key: order[key] for key in ('curve_id', 'target_price', 'risk_revision')}
    payload.update(stop_price=46290, breakeven_enabled=True)
    response = client.put(f"/api/trading/orders/{order['id']}/risk-settings", json=payload)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'confirmation_required'
    assert service._active_order(order['id'])['stop_price'] == order['stop_price']
    response = client.put(f"/api/trading/orders/{order['id']}/risk-settings", json={**payload, 'confirmed': True})
    assert response.status_code == 200, response.text
    assert service._exit_signal(service._active_order(order['id']), 46280) == 'stop'
    response = client.put(f"/api/trading/orders/{order['id']}/risk-settings", json=payload)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'risk_conflict'
    client.close()


def test_concurrent_edit_and_tick_never_overwrite_protection(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    def edit():
        try:
            return save(service, order, stop_price=46000)
        except trading.RiskConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        edit_future = pool.submit(edit)
        tick_future = pool.submit(tick, service, 46371)
        edit_future.result()
        tick_future.result()
    assert service._active_order(order['id'])['stop_price'] == 46275


def test_repeated_identical_save_does_not_invalidate_another_editor(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    saved = save(service, order, stop_price=46000)
    repeated = save(service, saved)
    assert repeated['risk_revision'] == saved['risk_revision']


def test_schema_migration_enables_existing_orders_idempotently(tmp_path):
    path = tmp_path / 'legacy.db'
    conn = db.connect(path)
    # Build the old table shape, including an existing inactive row's defaults.
    conn.execute('CREATE TABLE managed_orders (id TEXT PRIMARY KEY, code TEXT, contract_code TEXT, active INTEGER, simulation INTEGER)')
    conn.execute("INSERT INTO managed_orders VALUES ('old', 'TMFR1', 'TMFX6', 1, 1)")
    conn.commit()
    db.init_schema(conn)
    db.init_schema(conn)
    row = dict(conn.execute("SELECT * FROM managed_orders WHERE id = 'old'").fetchone())
    assert row['breakeven_enabled'] == 1 and row['risk_revision'] == 0
    assert row['breakeven_triggered_at'] is None and row['contract_tick'] == 1
    assert row['breakeven_trigger_points'] is None and row['breakeven_offset_points'] is None
    assert row['avg_exit_price'] is None
    conn.close()


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
@pytest.mark.parametrize('reenable', [False, True])
def test_configured_breakeven_points_reload_and_cover_tick_reenable_and_fill_sync(fake_service, action, direction, reenable):
    service, api, _ = fake_service
    order = filled(service, api, action)
    if reenable:
        order = save(service, order, breakeven_enabled=False)
    # A running service reads the edited file, including fractional point values.
    service.settings_path.write_text('{"breakeven_trigger_points": 40.5, "breakeven_offset_points": 2.5}')
    tick(service, 46270 + direction * 40.5)
    assert not service._active_order(order['id'])['breakeven_triggered_at']
    tick(service, 46270 + direction * 41)
    if reenable:
        assert not service._active_order(order['id'])['breakeven_triggered_at']
        save(service, order, breakeven_enabled=True)
    protected = service._active_order(order['id'])
    assert protected['stop_price'] == 46270 + direction * 3  # protection rounds toward the favorable tick
    assert protected['breakeven_triggered_at']
    status = service.status()
    assert status['breakeven_trigger_points'] == 40.5
    assert status['breakeven_offset_points'] == 2.5
    api.trades[0].status.deals = [SimpleNamespace(quantity=1, price=46270 + direction * 10)]
    service._persist_trade_state(order['id'], entry=api.trades[0])
    assert service._active_order(order['id'])['stop_price'] == 46270 + direction * 13
    service.settings_path.write_text('{"breakeven_trigger_points": 20, "breakeven_offset_points": 0}')
    service._persist_trade_state(order['id'], entry=api.trades[0])
    assert service._active_order(order['id'])['stop_price'] == 46270 + direction * 13


@pytest.mark.parametrize('contents', ['{}', '[]', 'broken json',
    '{"breakeven_trigger_points": true, "breakeven_offset_points": false}',
    '{"breakeven_trigger_points": -1, "breakeven_offset_points": -2}',
    '{"breakeven_trigger_points": "40", "breakeven_offset_points": null}',
    '{"breakeven_trigger_points": NaN, "breakeven_offset_points": Infinity}'])
def test_invalid_or_missing_breakeven_config_retains_compatible_defaults(fake_service, contents):
    service, _, _ = fake_service
    service.settings_path.write_text(contents)
    assert service.breakeven_settings() == {'breakeven_trigger_points': 100, 'breakeven_offset_points': 5}


def test_config_can_set_pure_breakeven_zero_offset(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    service.settings_path.write_text('{"breakeven_trigger_points": 20, "breakeven_offset_points": 0}')
    tick(service, 46291)
    assert service._active_order(order['id'])['stop_price'] == 46270


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_per_order_breakeven_points_are_isolated_and_persist(fake_service, action, direction):
    service, api, _ = fake_service
    first = filled(service, api, action)
    second = filled(service, api, action)
    first = save(service, first, breakeven_trigger_points=40, breakeven_offset_points=12)
    assert first['breakeven_trigger_points'] == 40 and first['breakeven_offset_points'] == 12
    assert service._public_order(service._active_order(second['id']))['breakeven_trigger_points'] == 100
    tick(service, 46270 + direction * 40)
    assert not service._active_order(first['id'])['breakeven_triggered_at']
    tick(service, 46270 + direction * 41)
    protected = service._active_order(first['id'])
    assert protected['stop_price'] == 46270 + direction * 12
    assert not service._active_order(second['id'])['breakeven_triggered_at']
    # Fill-average corrections also use this order's offset.
    api.trades[0].status.deals = [SimpleNamespace(quantity=1, price=46270 + direction * 10)]
    service._persist_trade_state(first['id'], entry=api.trades[0])
    assert service._active_order(first['id'])['stop_price'] == 46270 + direction * 22
    restarted = trading.TradingService(service.db_path, service.code, simulation=True, start_worker=False)
    try:
        saved = restarted._public_order(restarted._active_order(first['id']))
        assert saved['breakeven_trigger_points'] == 40 and saved['breakeven_offset_points'] == 12
    finally:
        restarted.close()


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_editing_latched_offset_only_tightens_and_legacy_edits_keep_points(fake_service, action, direction):
    service, api, _ = fake_service
    order = filled(service, api, action)
    order = save(service, order, breakeven_trigger_points=30, breakeven_offset_points=10)
    tick(service, 46270 + direction * 40)
    order = service._public_order(service._active_order(order['id']))
    order = save(service, order, breakeven_offset_points=20)
    assert order['stop_price'] == 46270 + direction * 20
    order = save(service, order, breakeven_trigger_points=200, breakeven_offset_points=0)
    assert order['stop_price'] == 46270 + direction * 20
    assert order['breakeven_triggered_at']
    updated = service.update_levels(order['id'], order['stop_price'], order['target_price'])
    assert updated['breakeven_trigger_points'] == 200 and updated['breakeven_offset_points'] == 0


def test_lowering_order_trigger_applies_on_save_without_changing_defaults(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    tick(service, 46290)
    updated = save(service, order, breakeven_trigger_points=10, breakeven_offset_points=0)
    assert updated['breakeven_triggered_at'] and updated['stop_price'] == 46270
    assert service.breakeven_settings()['breakeven_trigger_points'] == 100


@pytest.mark.parametrize('key', ['breakeven_trigger_points', 'breakeven_offset_points'])
@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True])
def test_invalid_per_order_points_rejected_without_writing(fake_service, key, value):
    service, api, _ = fake_service
    order = filled(service, api)
    with pytest.raises(ValueError, match='非負數'):
        save(service, order, **{key: value})
    assert service._active_order(order['id'])[key] is None


def test_dashboard_risk_api_saves_per_order_points(fake_service, monkeypatch):
    service, api, _ = fake_service
    order = filled(service, api)
    monkeypatch.setattr(dashboard, '_TRADING_SERVICE', service)
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', service.db_path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', service.code)
    payload = {key: order[key] for key in ('curve_id', 'stop_price', 'target_price', 'risk_revision')}
    payload.update(breakeven_enabled=True, breakeven_trigger_points=70, breakeven_offset_points=9)
    with TestClient(dashboard.app) as client:
        response = client.put(f"/api/trading/orders/{order['id']}/risk-settings", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['breakeven_trigger_points'] == 70
        assert response.json()['breakeven_offset_points'] == 9
        assert client.put(f"/api/trading/orders/{order['id']}/risk-settings",
                          json={**payload, 'breakeven_trigger_points': -1}).status_code == 422
        assert client.put(f"/api/trading/orders/{order['id']}/risk-settings",
                          json={**payload, 'breakeven_offset_points': True}).status_code == 422
