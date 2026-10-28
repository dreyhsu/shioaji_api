import json
from types import SimpleNamespace as NS
import pandas as pd
import pytest
import db
import market_health as mh
from mock_broker import MockBroker
from trading import TradingService
from test_market_health import calendar_document

pytestmark = pytest.mark.integration


class Transport(MockBroker):
    """Connection instances share an account, exactly as relogin must."""
    def __init__(self, account, clock):
        previous = account.get("position", 0)
        self.account = account
        super().__init__(clock=clock)
        self.position = previous
        self.price = account.get("price", 20000.)
        self.trades = account.setdefault("trades", [])
        self.down = lambda: None
        self.closed = False
        self.subscriptions = []
        self.quote.unsubscribe = lambda contract, **_: self.subscriptions.append(("remove", contract.code))

    @property
    def position(self):
        return self.account.get("position", 0)

    @position.setter
    def position(self, value):
        self.account["position"] = value

    def _subscribe(self, contract, **kwargs):
        self.subscriptions.append(("add", contract.code))
        super()._subscribe(contract, **kwargs)

    def set_session_down_callback(self, callback):
        self.down = callback

    def logout(self):
        self.closed = True


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "market.db"
    now, mono, account, apis = [pd.Timestamp("2026-10-27 19:07")], [0.], {}, []
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(calendar_document()))
    settings = mh.HealthSettings(calendar_path=str(calendar_path), trading_reconnect_enabled=True)
    health = mh.HealthPublisher(path, "monitor", "TMFR1", "simulation", clock=lambda: now[0])
    health.acquire()
    health.publish(state="healthy", ready_since="2026-10-27 19:00", bars={"ready": True, "last_minute_start": "2026-10-27 19:06"})
    def factory(_):
        api = Transport(account, lambda: now[0])
        apis.append(api)
        return api
    def create():
        return TradingService(path, "TMFR1", api_factory=factory, simulation=True, sdk=MockBroker.sdk,
                              clock=lambda: now[0], monotonic=lambda: mono[0], start_worker=False,
                              enable_telegram=False, health_enabled=True, health_settings=settings,
                              settings_path=tmp_path / "trading.json", alerts_path=tmp_path / "alerts.json")
    service = create()
    service.ensure_connected()
    service.process_market_update()
    service._risk_context = lambda _: (120., pd.DataFrame())
    assert service._broker_ready
    result = NS(service=service, now=now, mono=mono, account=account, apis=apis, create=create, health=health)
    yield result
    result.service.close()
    health.close()


def enter(service):
    return service.place_order({"contract_code": "TMF209912", "action": "Buy", "quantity": 1,
                                "price_type": "MKT", "order_type": "IOC", "simulation": True, "confirmed": True})


def test_reconnect_restores_exact_contract_and_ignores_old_callbacks(live):
    r = live
    order = enter(r.service)
    r.service.process_market_update()
    old = r.apis[0]
    old_callback = old._callback
    old.down()
    r.service.process_market_update()
    r.mono[0] += 6
    r.service.process_market_update()
    assert old.closed and len(r.apis) == 2
    assert r.service._broker_ready and r.service._wanted == {"TMF_MOCK"}
    assert ("add", "TMF_MOCK") in r.apis[1].subscriptions
    old_callback(NS(code="TMF_MOCK", close=1, datetime=r.now[0]))
    assert r.service._quotes["TMF_MOCK"]["price"] == 20000
    assert len(r.account["trades"]) == 1
    assert r.service._active_order(order["id"])["filled_quantity"] == 1


def test_one_fresh_contract_cannot_hide_another_stale_contract(live):
    service = live.service
    service.subscribe_contract("TMF_MOCK")
    service._wanted.add("TMF_OTHER")
    status = service.status()
    assert status["quote_contracts"]["TMF_MOCK"]["fresh"]
    assert not status["quote_connected"]
    live.mono[0] += 11
    assert service._fresh_risk_quote({"contract_code": "TMF_MOCK"}) is None


def test_stale_ticks_resubscribe_before_rebuilding_session(live):
    r = live
    enter(r.service)
    r.service.process_market_update()
    r.mono[0] += 61
    r.now[0] += pd.Timedelta(seconds=61)
    r.service.process_market_update()
    assert len(r.apis) == 1
    assert ("remove", "TMF_MOCK") in r.apis[0].subscriptions
    assert r.service._quote_health()["TMF_MOCK"]["fresh"]


def test_repeated_delivery_of_an_old_exchange_tick_cannot_mask_a_stall(live):
    r = live
    enter(r.service)
    r.service.process_market_update()
    old_stamp = r.now[0]
    r.mono[0] += 61
    r.now[0] += pd.Timedelta(seconds=61)
    r.apis[0]._callback(NS(code="TMF_MOCK", close=20000, datetime=old_stamp))
    assert not r.service._quote_health()["TMF_MOCK"]["fresh"]
    r.service.process_market_update()
    assert ("remove", "TMF_MOCK") in r.apis[0].subscriptions
    assert len(r.apis) == 1


def test_accepted_exit_with_lost_response_never_resends_after_reconnect_or_restart(live):
    r = live
    order = enter(r.service)
    r.service.process_market_update()
    api = r.apis[0]
    submit = api.place_order
    def accepted_then_timeout(contract, request):
        submit(contract, request)
        raise TimeoutError("accepted but response lost")
    api.place_order = accepted_then_timeout
    r.service._try_auto_exit(r.service._active_order(order["id"]), "stop")
    assert len(r.account["trades"]) == 2 and r.account["position"] == 0
    assert r.service._active_order(order["id"])["exit_status"] == "unknown"
    api.down()
    r.service.process_market_update()
    r.mono[0] += 6
    r.service.process_market_update()
    assert not r.service._broker_ready
    for _ in range(3):
        r.service.process_market_update()
    r.service.close()
    r.service = r.create()
    r.service.ensure_connected()
    r.service.process_market_update()
    assert not r.service._broker_ready and len(r.account["trades"]) == 2


def test_entry_intent_blocks_repeat_when_broker_accepts_but_response_is_lost(live):
    r = live
    api = r.apis[0]
    submit = api.place_order
    def accepted_then_timeout(contract, request):
        submit(contract, request)
        raise TimeoutError("response lost")
    api.place_order = accepted_then_timeout
    with pytest.raises(RuntimeError, match="委託狀態不明"):
        enter(r.service)
    assert r.service._active_orders()[0]["entry_status"] == "unknown"
    with pytest.raises(ValueError, match="結果不明"):
        enter(r.service)
    assert len(r.account["trades"]) == 1
    r.service.close()
    r.service = r.create()
    r.service.ensure_connected()
    r.service.process_market_update()
    with pytest.raises(ValueError, match="結果不明"):
        enter(r.service)
    assert not r.service._broker_ready and len(r.account["trades"]) == 1


def test_fixed_stop_works_with_stale_bars_but_new_entry_and_curve_exit_do_not(live):
    r = live
    order = enter(r.service)
    r.service.process_market_update()
    r.health.publish(state="backfilling", bars={"ready": False})
    assert r.service._curve_close_break({}) == "curve_unavailable"
    with pytest.raises(ValueError, match="行情回補"):
        enter(r.service)
    r.apis[0].publish(19000)
    r.service.process_market_update()
    row = r.service._active_order(order["id"])
    assert row["exit_status"] == "Filled" and row["trigger_reason"] == "stop"
    assert len(r.account["trades"]) == 2


def test_position_mismatch_never_claims_recovery(live):
    r = live
    enter(r.service)
    r.service.process_market_update()
    r.account["position"] = 2  # an external order changed the broker position
    r.apis[0].down()
    r.service.process_market_update()
    r.mono[0] += 6
    r.service.process_market_update()
    assert not r.service._broker_ready and "不一致" in r.service._reconcile_error
    assert len(r.account["trades"]) == 1


def test_unmanaged_working_order_prevents_recovery_even_when_flat(live):
    r = live
    r.account["trades"].append(NS(order=NS(id="external"), status=NS(status="Submitted")))
    r.service._broker_ready = False
    r.service._reconcile_required = True
    r.service.process_market_update()
    assert not r.service._broker_ready and "未管理" in r.service._reconcile_error


def test_mode_cannot_change_during_automatic_recovery(live):
    r = live
    r.service._simulation = False
    with pytest.raises(ValueError, match="環境已改變"):
        r.service.process_market_update()
    assert len(r.apis) == 1


def test_initial_login_failure_is_retried_without_a_browser_request(live):
    r = live
    r.service._close_api()
    factory = r.service.api_factory
    def failed_factory(_):
        raise RuntimeError("NotReady SessionNotEstablished")
    r.service.api_factory = failed_factory
    with pytest.raises(RuntimeError):
        r.service.ensure_connected()
    r.service.process_market_update()
    assert r.service._recovery.pending and not r.service._broker_ready
    r.service.api_factory = factory
    r.mono[0] += 6
    r.service.process_market_update()
    assert r.service._api is not None and r.service._broker_ready


def test_entry_baseline_advances_after_outage_instead_of_replaying_old_signal(live):
    r = live
    entry = {"id": 99, "check_from": "2026-10-27 18:00"}
    updates = []
    r.service.curve_entry._update = lambda row_id, **changes: updates.append(changes)
    r.health.publish(state="backfilling", bars={"ready": False})
    assert not r.service._entry_data_ready(entry)
    r.health.publish(state="healthy", ready_since="2026-10-27 19:06:30", bars={"ready": True, "last_minute_start": "2026-10-27 19:06"})
    assert not r.service._entry_data_ready(entry)
    assert updates[-1]["check_from"] == "2026-10-27 19:07:00"


def test_timezone_aware_tick_stays_on_taipei_clock(live):
    r = live
    r.service.subscribe_contract("TMF_MOCK")
    r.apis[0].price = 20000
    r.apis[0]._callback(NS(code="TMF_MOCK", close=20001,
                          datetime=pd.Timestamp("2026-10-27 11:07", tz="UTC")))
    assert r.service._quotes["TMF_MOCK"]["at"] == r.now[0]
    assert r.service._quote_health()["TMF_MOCK"]["fresh"]
