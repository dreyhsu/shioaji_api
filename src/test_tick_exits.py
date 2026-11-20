"""Protective exits require live, published ticks, independently of historical readiness."""
import json

import pandas as pd
import pytest

import db
import market_health as mh
from test_independent_trade import rig, request

pytestmark = pytest.mark.integration


def position(r, action='Buy'):
    request(r, payload=dict(contract_code='TMF209912', action=action, quantity=1,
        price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True))
    r.runtime.step()
    r.runtime.step()
    return r.service.orders()[0]


def history_missing(r):
    for code in ('TMFR1', 'TMF209912'):
        r.health[code].publish(state='degraded', bars=dict(ready=False, blocking_missing_count=1),
                               stream_error='history_not_bridged', validation_pending=True)
    with db.session(r.path) as conn:
        conn.execute("DELETE FROM minute_bars WHERE code='TMF209912'")
        conn.commit()


def tape(r, prices, code='TMF209912'):
    r.store.quotes([], [dict(tick_id=f'{code}:{r.now[0]}:{i}', code=code, mode='simulation', price=price,
        quote_at=str(r.now[0] - pd.Timedelta(milliseconds=len(prices) - i)), received_at=str(r.now[0]),
        instance='offline-monitor', generation=1) for i, price in enumerate(prices)])


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
@pytest.mark.parametrize('reason', ['stop', 'target'])
def test_fixed_tick_exit_retains_intermediate_hit_with_missing_history(rig, action, direction, reason):
    r = rig
    order = position(r, action)
    r.update('2026-10-30 09:02:05', tick=20000)
    history_missing(r)
    tape(r, [order['stop_price'] if reason == 'stop' else order['target_price'], 20000])
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert len(r.broker.trades) == 2 and closed['trigger_reason'] == reason
    assert closed['exit_status'] == 'Filled' and not closed['active']
    r.runtime.step()
    assert len(r.broker.trades) == 2


@pytest.mark.parametrize('failure', ['generation', 'instance', 'tick_down', 'tick_stale', 'monitor_stale', 'receipt'])
def test_missing_history_does_not_bypass_live_exit_health(rig, failure):
    r = rig
    order = position(r)
    r.update('2026-10-30 09:02:05', tick=order['stop_price'] - 1)
    history_missing(r)
    path = r.path.parent / '.health' / r.path.name / 'tick-fetcher.json'
    health = mh.read_health(path)
    if failure in ('generation', 'instance'):
        health[failure] = 2 if failure == 'generation' else 'other'
    elif failure == 'tick_down':
        health['state'] = 'failed'
    elif failure == 'tick_stale':
        health['heartbeat_at'] = '2026-10-30 09:01:00'
    elif failure == 'monitor_stale':
        r.health['TMF209912'].publish(cycle_completed_at='2026-10-30 09:01:00')
    else:
        with db.session(r.path) as conn:
            conn.execute("UPDATE market_latest_quotes SET received_at='2026-10-30 09:01:00'")
            conn.commit()
    mh.atomic_json(path, health)
    r.runtime.step()
    assert len(r.broker.trades) == 1
    assert not r.service.orders()[0]['protection']['fixed_stop']


def test_fixed_breakeven_peak_and_retrace_use_ticks_despite_missing_history(rig):
    r = rig
    order = position(r)
    with db.session(r.path) as conn:
        conn.execute('UPDATE managed_orders SET breakeven_enabled=1,breakeven_trigger_points=100,'
                     'breakeven_offset_points=5 WHERE id=?', (order['id'],))
        conn.commit()
    r.update('2026-10-30 09:02:05', tick=20100)
    history_missing(r)
    tape(r, [20101, 20005, 20100])
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert closed['breakeven_price'] == 20005 and closed['trigger_reason'] == 'stop'
    assert len(r.broker.trades) == 2 and not closed['active']


@pytest.mark.parametrize('failure', ['none', 'stale', 'generation'])
def test_curve_tick_exit_uses_signal_tape_and_preserves_fixed_stop(rig, failure):
    r = rig
    order = position(r)
    with db.session(r.path) as conn:
        anchors = [dict(time=int(pd.Timestamp(stamp).value // 10**9), price=20000)
                   for stamp in ('2026-10-30 08:55', '2026-10-30 08:56')]
        curve = db.insert_trendline(conn, 'TMFR1', anchors, kind='line', timeframe='1m')
        conn.execute('UPDATE managed_orders SET curve_id=?,target_price=NULL WHERE id=?', (curve, order['id']))
        conn.commit()
    r.update('2026-10-30 09:02:05', signal_close=20001, tick=20001)
    history_missing(r)
    tape(r, [19999, 20001], code='TMFR1')
    if failure == 'stale':
        r.health['TMFR1'].publish(cycle_completed_at='2026-10-30 09:01:00')
    elif failure == 'generation':
        with db.session(r.path) as conn:
            conn.execute("UPDATE market_latest_quotes SET generation=2 WHERE code='TMFR1'")
            conn.commit()
    r.runtime.step()
    r.runtime.step()
    if failure == 'none':
        assert len(r.broker.trades) == 2 and r.service.orders()[0]['trigger_reason'] == 'curve'
    else:
        assert len(r.broker.trades) == 1
        assert r.service.orders()[0]['protection']['fixed_stop']
        r.store.quotes([dict(code='TMF209912',mode='simulation',price=order['stop_price']-1,volume=1,
            quote_at=str(r.now[0]),received_at=str(r.now[0]),instance='offline-monitor',generation=1)])
        r.runtime.step()
        r.runtime.step()
        assert len(r.broker.trades) == 2 and r.service.orders()[0]['trigger_reason'] == 'stop'


def test_automatic_entry_retains_contract_history_gate(rig):
    from test_independent_trade import arm_curve
    r = rig
    arm_curve(r, price=20000)
    r.update('2026-10-30 09:03:03', close=20001)
    r.health['TMF209912'].publish(state='degraded', bars=dict(ready=False))
    r.runtime.step()
    assert not r.broker.trades
    assert r.service.curve_entry.snapshot()['state'] == 'armed'


@pytest.mark.parametrize('backlog', ['invalid_signal_ticks', 'contract_ticks'])
def test_curve_tape_makes_progress_with_a_bounded_backlog(rig, backlog):
    r = rig
    order = position(r)
    with db.session(r.path) as conn:
        anchors = [dict(time=int(pd.Timestamp(stamp).value // 10**9), price=20000)
                   for stamp in ('2026-10-30 08:55', '2026-10-30 08:56')]
        curve = db.insert_trendline(conn, 'TMFR1', anchors, kind='line', timeframe='1m')
        conn.execute('UPDATE managed_orders SET curve_id=?,target_price=NULL WHERE id=?', (curve, order['id']))
        conn.commit()
    r.update('2026-10-30 09:02:05', tick=20001)
    history_missing(r)
    if backlog == 'invalid_signal_ticks':
        tape(r, [20001] * 129 + [19999, 20001], code='TMFR1')
        with db.session(r.path) as conn:
            conn.execute("UPDATE market_risk_ticks SET generation=2 WHERE code='TMFR1' AND seq<=128")
            conn.commit()
        r.runtime.step()
        assert len(r.broker.trades) == 1
        r.runtime.step()
    else:
        tape(r, [20001] * 130)
        tape(r, [19999, 20001], code='TMFR1')
        r.runtime.step()
    r.runtime.step()
    assert len(r.broker.trades) == 2
    assert r.service.orders()[0]['trigger_reason'] == 'curve'
