"""Persistent risk changes, fresh-tick protection and HTTP conflict semantics."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard
import db
import trading
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
