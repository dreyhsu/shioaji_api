import json

import pytest

import db
from entry_notify import EntryNotifier
import telegram_notify as tg


@pytest.fixture
def notifier(tmp_path, monkeypatch):
    path = tmp_path / "alerts.json"
    path.write_text('{"telegram_enabled": true}')
    messages = []
    monkeypatch.setattr(tg, "send_message", lambda message: messages.append(message) or True)
    notifier = EntryNotifier(tmp_path / "alerts.db", alerts_path=path, dispatch=lambda task: task())
    return notifier, messages


def order(**changes):
    return dict(dict(id="order-1", code="TMFR1", contract_code="TMFX6", action="Buy",
                     simulation=True, filled_quantity=1, quantity=3, avg_fill_price=25000.5,
                     atr=100, stop_price=24800.5, target_price=25200.5, curve_id=None,
                     entry_trade_id="broker-1", created_at="2026-10-27 09:00:00",
                     updated_at="2026-10-27 09:01:00"), **changes)


@pytest.mark.parametrize("action,simulation,label,mode", [
    ("Buy", True, "買進做多", "模擬環境"), ("Sell", False, "賣出做空", "正式環境"),
])
def test_fill_message_and_persisted_delivery(notifier, action, simulation, label, mode):
    notifier, messages = notifier
    notifier.notify(order(action=action, simulation=simulation))
    assert len(messages) == 1
    assert f"{label}進場成交" in messages[0] and mode in messages[0]
    for text in ("TMFX6", "1 / 3 口", "25,000.50", "24,800.50", "25,200.50", "broker-1"):
        assert text in messages[0]
    with db.session(notifier.db_path) as conn:
        alert = db.load_alerts(conn, "TMFR1").iloc[0]
    assert alert["kind"] == "entry_fill" and alert["ok"] == 1


def test_deduplication_survives_restart_and_allows_additional_partial_fill(notifier):
    notifier, messages = notifier
    notifier.notify(order())
    notifier.notify(order(updated_at="2026-10-27 09:02:00"))
    restarted = EntryNotifier(notifier.db_path, alerts_path=notifier.config.path, dispatch=lambda task: task())
    restarted.notify(order())
    restarted.notify(order(filled_quantity=3))
    assert len(messages) == 2
    assert "3 / 3 口" in messages[1]


def test_switch_reloads_and_disabled_notifications_do_not_claim_alerts(notifier):
    notifier, messages = notifier
    notifier.config.path.write_text(json.dumps({"telegram_enabled": False, "_comment": "pause"}))
    notifier.notify(order())
    assert not messages
    with db.session(notifier.db_path) as conn:
        assert db.load_alerts(conn, "TMFR1").empty
    notifier.config.path.write_text('{"telegram_enabled": true}')
    notifier.notify(order(filled_quantity=3))
    assert len(messages) == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("offline")])
def test_delivery_failure_is_recorded_without_raising_or_resending(notifier, monkeypatch, failure):
    notifier, messages = notifier
    def fail(message):
        messages.append(message)
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(tg, "send_message", fail)
    notifier.notify(order())
    notifier.notify(order())
    assert len(messages) == 1
    with db.session(notifier.db_path) as conn:
        assert db.load_alerts(conn, "TMFR1").iloc[0]["ok"] == 0


def test_dispatch_is_deferred_and_mock_can_disable_it(notifier):
    notifier, messages = notifier
    tasks = []
    notifier.dispatch = tasks.append
    notifier.enabled = False
    notifier.notify(order())
    assert not tasks
    notifier.enabled = True
    notifier.notify(order())
    assert len(tasks) == 1 and not messages
    tasks[0]()
    assert len(messages) == 1


def test_html_is_escaped_and_curve_and_missing_price_are_explicit():
    message = tg.format_entry_fill(order(contract_code="<TMF>", entry_trade_id="a&b", curve_id=1,
                                         avg_fill_price=None, target_price=None))
    assert "&lt;TMF&gt;" in message and "a&amp;b" in message
    assert "待券商回報" in message and "停利：曲線停利" in message
    assert tg.FOOTER not in message
