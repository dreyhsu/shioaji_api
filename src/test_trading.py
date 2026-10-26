from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import db
import trading


def test_two_atr_levels_follow_long_and_short_direction():
    assert trading.stop_target_prices("Buy", 20000, 100) == (19800, 20200)
    assert trading.stop_target_prices("Sell", 20000, 100) == (20200, 19800)
    assert trading.crossed_exit("Buy", 19799, 19800, 20200) == "stop"
    assert trading.crossed_exit("Buy", 20201, 19800, 20200) == "target"
    assert trading.crossed_exit("Sell", 20201, 20200, 19800) == "stop"
    assert trading.crossed_exit("Sell", 19799, 20200, 19800) == "target"
    assert trading.crossed_exit("Buy", 20000, 19800, 20200) is None


def test_curve_take_profit_requires_one_minute_close_to_cross_hourly_curve():
    assert trading.crossed_curve_close("Buy", 101, 100, 99, 100)
    assert not trading.crossed_curve_close("Buy", 99, 100, 98, 100)
    assert trading.crossed_curve_close("Sell", 99, 100, 101, 100)
    assert not trading.crossed_curve_close("Sell", 101, 100, 102, 100)


class FakeContract:
    code = "TMFX6"
    symbol = "TMF202611"
    target_code = "TMFX6"
    delivery_month = "202611"
    delivery_date = "2026/11/18"
    name = "微型臺指期貨 202611"
    tick = 1


class FakeTrade:
    def __init__(self, order_id, status="PendingSubmit"):
        self.order = SimpleNamespace(id=order_id, price=46270)
        self.status = SimpleNamespace(
            id=order_id, status=status, deal_quantity=0, deals=[], modified_price=0
        )


class FakeApi:
    def __init__(self):
        self.futopt_account = object()
        self.Contracts = SimpleNamespace(
            Futures=SimpleNamespace(TMF=[FakeContract()])
        )
        self.quote = SimpleNamespace(subscribe=lambda *args, **kwargs: None)
        self.sent_orders = []
        self.trades = []
        self.cancelled = []
        self.set_on_tick_fop_v1_callback = lambda callback: None

    def place_order(self, contract, order):
        self.sent_orders.append((contract, order))
        trade = FakeTrade(f"paper-{len(self.sent_orders)}")
        self.trades.append(trade)
        return trade

    def margin(self, account):
        return SimpleNamespace(
            status="Fetched", risk_indicator=132.5, maintenance_margin=18000,
            initial_margin=22000, available_margin=50000, equity=71000,
        )

    def update_status(self, account):
        return None

    def list_trades(self):
        return self.trades

    def cancel_order(self, trade):
        self.cancelled.append(trade)
        trade.status.status = "Cancelled"
        return trade


@pytest.fixture
def fake_service(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    with db.session(db_path):
        pass
    api = FakeApi()
    factory_modes = []

    def factory(simulation):
        factory_modes.append(simulation)
        return api

    enum = lambda **values: SimpleNamespace(**values)
    monkeypatch.setattr(trading, "sj", SimpleNamespace(
        Action=enum(Buy="Buy", Sell="Sell"),
        FuturesPriceType=enum(LMT="LMT", MKT="MKT", MKP="MKP"),
        OrderType=enum(ROD="ROD", IOC="IOC", FOK="FOK"),
        FuturesOCType=enum(Auto="Auto", Cover="Cover"),
        QuoteType=enum(Tick="Tick"), QuoteVersion=enum(v1="v1"),
        Order=lambda **kwargs: SimpleNamespace(**kwargs),
    ))
    monkeypatch.setenv("SHIOAJI_SIMULATION", "1")
    service = trading.TradingService(
        db_path, "TMFR1", settings_path=tmp_path / "trading.json",
        api_factory=factory, start_worker=False,
    )
    service._risk_context = lambda code: (120.0, pd.DataFrame())
    yield service, api, factory_modes
    service.close()


def test_simulation_order_and_margin_are_exposed(fake_service):
    service, api, factory_modes = fake_service
    assert service.status()["mode"] == "模擬環境"
    assert factory_modes == [True]
    contracts = service.contracts()
    assert contracts[0]["delivery_month"] == "202611"

    result = service.place_order({
        "confirmed": True,
        "simulation": True,
        "contract_code": "TMF202611",
        "action": "Buy",
        "quantity": 1,
        "price": 46270,
        "price_type": "LMT",
        "order_type": "ROD",
        "curve_id": None,
    })
    assert result["entry_trade_id"] == "paper-1"
    assert result["stop_price"] == 46030
    assert result["target_price"] == 46510
    sent = api.sent_orders[-1][1]
    assert sent.price == 46270
    assert sent.quantity == 1
    assert sent.price_type == "LMT"
    assert sent.order_type == "ROD"
    assert sent.octype == "Auto"

    filled = FakeTrade("paper-1", status="Filled")
    filled.status.deal_quantity = 1
    filled.status.deals = [SimpleNamespace(quantity=1, price=46280)]
    service._persist_trade_state(result["id"], entry=filled)
    updated = service._active_order(result["id"])
    assert updated["avg_fill_price"] == 46280
    assert updated["stop_price"] == 46040
    assert updated["target_price"] == 46520

    margin = service.margin()
    assert margin["risk_indicator"] == 132.5
    assert margin["maintenance_margin"] == 18000


def test_order_rejects_missing_confirmation_and_wrong_mode(fake_service):
    service, _api, _modes = fake_service
    request = {
        "confirmed": False, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": 1, "price": 46270,
        "price_type": "LMT", "order_type": "ROD",
    }
    with pytest.raises(ValueError, match="確認"):
        service.place_order(request)
    request.update(confirmed=True, simulation=False)
    with pytest.raises(ValueError, match="環境已改變"):
        service.place_order(request)


def test_pending_entry_can_be_cancelled(fake_service):
    service, api, _modes = fake_service
    result = service.place_order({
        "confirmed": True, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": 1, "price": 46270,
        "price_type": "LMT", "order_type": "ROD",
    })
    cancelled = service.cancel_entry(result["id"])
    assert api.cancelled == [api.trades[0]]
    assert cancelled["entry_status"] == "Cancelled"
    assert cancelled["filled_quantity"] == 0
    assert cancelled["active"] == 0


def test_filled_position_can_switch_between_fixed_and_curve_take_profit(fake_service):
    service, api, _modes = fake_service
    result = service.place_order({
        "confirmed": True, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": 1, "price": 46270,
        "price_type": "LMT", "order_type": "ROD",
    })
    entry = api.trades[0]
    entry.status.status = "Filled"
    entry.status.deal_quantity = 1
    entry.status.deals = [SimpleNamespace(quantity=1, price=46270)]
    service._persist_trade_state(result["id"], entry=entry)

    anchors = [
        {"time": int(pd.Timestamp(stamp).value // 10**9), "price": price}
        for stamp, price in zip(
            ("2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45"),
            (46000, 46100, 46200),
        )
    ]
    with db.session(service.db_path) as conn:
        curve_id = db.insert_trendline(conn, "TMFR1", anchors, kind="curve")
    changed = service.set_order_curve(result["id"], curve_id)
    assert changed["curve_id"] == curve_id
    assert changed["stop_price"] == 46030  # original -2 ATR protection stays in place
    assert changed["target_price"] is None  # curve replaces the fixed +2 ATR target
    assert changed["curve_check_from"]

    restored = service.set_order_curve(result["id"], None)
    assert restored["curve_id"] is None
    assert restored["stop_price"] == 46030
    assert restored["target_price"] == 46510


def test_curve_monitor_uses_latest_closed_minute_and_hourly_curve(fake_service, monkeypatch):
    service, _api, _modes = fake_service
    monkeypatch.setattr(trading.market_clock, "now", lambda: pd.Timestamp("2026-10-05 11:03:00"))
    stamps = [
        "2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45",
        "2026-10-05 11:01", "2026-10-05 11:02",
    ]
    closes = [100, 100, 100, 101, 99]
    with db.session(service.db_path) as conn:
        for stamp, close in zip(stamps, closes):
            conn.execute(
                "INSERT INTO minute_bars (code, minute_start, session_id, open, high, low, close, volume, amount) "
                "VALUES ('TMFR1', ?, '2026-10-05 day', ?, ?, ?, ?, 1, 0)",
                (stamp, close, close, close, close),
            )
        anchors = [
            {"time": int(pd.Timestamp(stamp).value // 10**9), "price": 100}
            for stamp in stamps[:3]
        ]
        curve_id = db.insert_trendline(conn, "TMFR1", anchors, kind="curve")
    row = {
        "id": "minute-curve-test", "code": "TMFR1", "curve_id": curve_id,
        "curve_check_from": "2026-10-05 08:45:00", "action": "Buy",
    }
    assert service._curve_close_break(row) == "curve"


def test_partial_entry_is_cancelled_before_market_exit(fake_service):
    service, api, _modes = fake_service
    result = service.place_order({
        "confirmed": True, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": 2, "price": 46270,
        "price_type": "LMT", "order_type": "ROD",
    })
    entry = api.trades[0]
    entry.status.deal_quantity = 1
    entry.status.deals = [SimpleNamespace(quantity=1, price=46270)]
    entry.status.status = "PartFilled"
    service._persist_trade_state(result["id"], entry=entry)
    row = service._active_order(result["id"])

    service._try_auto_exit(row, "stop")
    assert api.cancelled == [entry]
    assert len(api.sent_orders) == 1  # no close is sent until the remainder settles

    entry.status.status = "Cancelled"
    service._persist_trade_state(result["id"], entry=entry)
    row = service._active_order(result["id"])
    service._try_auto_exit(row, "stop")
    assert len(api.sent_orders) == 2
    close = api.sent_orders[-1][1]
    assert close.quantity == 1
    assert close.price_type == "MKT"
    assert close.octype == "Cover"


def test_form_displays_the_selected_production_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIOAJI_SIMULATION", "0")
    api = FakeApi()
    modes = []
    service = trading.TradingService(
        tmp_path / "production.db", "TMFR1",
        api_factory=lambda simulation: (modes.append(simulation) or api),
        start_worker=False,
    )
    try:
        status = service.status()
        assert status["simulation"] is False
        assert status["mode"] == "正式環境"
        assert modes == [False]
    finally:
        service.close()


def test_auto_exit_setting_defaults_on_and_can_be_disabled(tmp_path):
    settings = tmp_path / "trading.json"
    service = trading.TradingService(tmp_path / "unused.db", "TMFR1", settings_path=settings)
    assert service.auto_exit_enabled() is True
    settings.write_text('{"auto_exit_enabled": false}', encoding="utf-8")
    assert service.auto_exit_enabled() is False
