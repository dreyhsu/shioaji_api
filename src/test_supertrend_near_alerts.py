"""Real standard-ST calculations and persistent claims, without network I/O."""
import json

import pandas as pd
import pytest

import alert_rules as ar
import db
import monitor
import session_bars as sb
import supertrend_near_alerts as near
from supertrend import confirmed_supertrend


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'alerts.json'
    settings = {name: {'enabled': False} for name in ar.RULES}
    settings.update({name: {'enabled': False} for name in (
        'gap_formed', 'gap_entered', 'gap_filled', 'supertrend_opposite')})
    settings['supertrend_near'] = dict(enabled=True, distance_percent=0.5, cooldown_minutes=10)
    path.write_text(json.dumps(settings), encoding='utf-8')
    return ar.AlertConfig(path)


def market(direction=-1):
    stamps = pd.date_range('2026-11-18 15:00', periods=14, freq='h')
    hours = pd.DataFrame(dict(timestamp=stamps, hour_start=stamps,
        session_id='2026-11-19 night', open=10000., high=10100., low=9900.,
        close=10000., volume=60., minutes=60))
    if direction == 1:
        hours.loc[13, ['high', 'close']] = [11100., 11000.]
    st = confirmed_supertrend(hours, '2026-11-19 08:45').iloc[-1]
    assert st.supertrend_direction == direction
    return hours, float(st.supertrend)


def minute(close, stamp='2026-11-19 08:50'):
    return pd.Series(dict(minute_start=pd.Timestamp(stamp),
        open=close, high=close, low=close, close=close, volume=1., session_id='2026-11-19 day'))


def hits(hours, bar, config, *, evaluator=None, now=None):
    return (evaluator or near.SupertrendNearAlerts()).candidates(
        hours, 'TMFR1', bar, now if now is not None else bar.minute_start + near.MINUTE,
        config, mode='production')


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('distance,expected', [(0, True), (.49, True), (.5, True), (.51, False), (-.01, False)])
def test_symmetric_proximity_touch_threshold_and_wrong_side(config, direction, distance, expected):
    hours, line = market(direction)
    result = hits(hours, minute(line * (1 + direction * distance / 100)), config)
    assert bool(result) == expected
    if expected:
        hit = result[0]
        assert hit.rule == 'supertrend_near' and not hit.once
        assert f'production:1h:{direction}' in hit.key
        assert ('空頭' if direction == -1 else '多頭') in hit.message
        assert f'{abs(distance):.4f}%' in hit.message
        assert '指標收定時間：2026-11-19 05:00' in hit.message
        assert '確認時間：2026-11-19 08:51（Asia/Taipei）' in hit.message


def test_wick_is_not_close(config):
    hours, line = market()
    bar = minute(line * .98)
    bar['high'] = line
    assert hits(hours, bar, config) == []


@pytest.mark.parametrize('stamp', [
    '2026-11-19 08:45', '2026-11-19 09:44', '2026-11-19 13:44',
    '2026-11-19 15:00', '2026-11-20 00:00', '2026-11-20 04:59'])
def test_day_night_opens_hour_boundary_midnight_and_session_ends(config, stamp):
    end = pd.Timestamp(stamp) + near.MINUTE
    minutes = pd.DataFrame(dict(
        minute_start=pd.date_range('2026-11-17 15:00', end - near.MINUTE, freq='min'),
        open=10000., high=10100., low=9900., close=10000., volume=1.))
    hours = sb.minute_bars_to_hourly(minutes)
    reading = confirmed_supertrend(hours, end).iloc[-1]
    assert hits(hours, minute(float(reading.supertrend), stamp), config)


@pytest.mark.parametrize('seconds,expected', [(-.001, False), (0, True), (59.999, True), (60, False), (3600, False)])
def test_closed_live_minute_window(config, seconds, expected):
    hours, line = market()
    bar = minute(line)
    now = bar.minute_start + near.MINUTE + pd.Timedelta(seconds=seconds)
    assert bool(hits(hours, bar, config, now=now)) == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 0, -1])
def test_invalid_price_does_not_notify(config, value):
    assert hits(market()[0], minute(value), config) == []


def test_missing_partial_or_unwarmed_hour_does_not_borrow_old_trend(config):
    hours, line = market()
    assert hits(hours, minute(line, '2026-11-19 09:50'), config) == []
    partial = hours.iloc[-1:].copy()
    partial['timestamp'] = pd.Timestamp('2026-11-19 08:45')
    partial['hour_start'] = partial.timestamp
    partial['session_id'] = '2026-11-19 day'
    partial['minutes'] = 59
    assert hits(pd.concat([hours, partial], ignore_index=True),
                minute(line, '2026-11-19 09:50'), config) == []
    assert hits(hours.tail(5).reset_index(drop=True), minute(line), config) == []
    assert hits(hours.iloc[:-1], minute(line), config) == []
    # Missing a minute in the last night hour invalidates its reading as well.
    hours.loc[13, 'minutes'] = 59
    assert hits(hours, minute(line), config) == []


def test_forming_hour_is_ignored_and_cache_invalidates_on_closed_correction(config, monkeypatch):
    hours, line = market()
    forming = hours.iloc[-1:].copy()
    forming['timestamp'] = pd.Timestamp('2026-11-19 08:45')
    forming['hour_start'] = forming.timestamp
    forming['session_id'] = '2026-11-19 day'
    forming['minutes'] = 6
    hours = pd.concat([hours, forming], ignore_index=True)
    calls = []
    calculate = near.confirmed_supertrend
    monkeypatch.setattr(near, 'confirmed_supertrend', lambda *args: calls.append(1) or calculate(*args))
    evaluator = near.SupertrendNearAlerts()
    assert hits(hours, minute(line), config, evaluator=evaluator)
    hours.loc[14, ['high', 'close']] = 20000.
    assert hits(hours, minute(line), config, evaluator=evaluator)
    assert len(calls) == 1
    hours.loc[0, 'high'] += 50
    hits(hours, minute(line), config, evaluator=evaluator)
    assert len(calls) == 2
    # The first day hour becomes available precisely at 09:45 and flips ST.
    hours.loc[14, 'minutes'] = 60
    end = pd.Timestamp('2026-11-19 09:45')
    new = calculate(hours, end).iloc[-1]
    assert new.supertrend_direction == 1
    result = hits(hours, minute(float(new.supertrend), '2026-11-19 09:44'), config, evaluator=evaluator)
    assert result and '多頭' in result[0].message
    assert '指標收定時間：2026-11-19 09:45' in result[0].message
    assert len(calls) == 3


@pytest.mark.parametrize('switch', ['telegram_enabled', 'supertrend_near'])
def test_switches(config, switch):
    if switch == 'telegram_enabled':
        config.data[switch] = False
    else:
        config.data[switch]['enabled'] = False
    hours, line = market()
    assert hits(hours, minute(line), config) == []


@pytest.mark.parametrize('field,value', [
    ('distance_percent', -1), ('distance_percent', float('nan')),
    ('distance_percent', float('inf')), ('distance_percent', True),
    ('distance_percent', '0.5'), ('distance_percent', None),
    ('cooldown_minutes', -1), ('cooldown_minutes', float('inf')),
    ('cooldown_minutes', False), ('cooldown_minutes', None), ('enabled', 'true')])
def test_bad_config_keeps_entire_previous_config(config, field, value):
    before = json.loads(json.dumps(config.data))
    config.path.write_text(json.dumps({'supertrend_near': {field: value}}), encoding='utf-8')
    assert not config.reload()
    assert f'supertrend_near.{field}' in config.last_error
    assert config.data == before


def test_threshold_reload_changes_next_minute_without_recomputing_st(config):
    hours, line = market()
    evaluator = near.SupertrendNearAlerts()
    assert not hits(hours, minute(line * .994), config, evaluator=evaluator)
    config.path.write_text(json.dumps({'supertrend_near': {'distance_percent': .7, 'cooldown_minutes': 2}}), encoding='utf-8')
    assert config.reload()
    assert config.cooldown_minutes(near.RULE) == 2
    assert hits(hours, minute(line * .994), config, evaluator=evaluator)


def state_for(hours, bar):
    return monitor.MarketState(pd.DataFrame([bar]), hours, pd.DataFrame(), len(hours)-1,
                               None, bar, None, hours.iloc[-1])


@pytest.mark.parametrize('cooldown,expected', [(10, 2), (0, 4)])
def test_monitor_claim_delivery_cooldown_repoll_and_restart(config, tmp_path, monkeypatch, cooldown, expected):
    config.data['supertrend_near']['cooldown_minutes'] = cooldown
    path = tmp_path / 'market.db'
    cfg = monitor.MonitorConfig(db_path=path, alerts_config=config.path, mode='production')
    hours, line = market()
    queued = []

    class Notifier:
        def set_enabled(self, enabled):
            assert enabled

        def submit(self, alert_id, message):
            queued.append((alert_id, message))
            return True

    for i, stamp in enumerate(['08:50', '08:51', '08:59', '09:00']):
        bar = minute(line, '2026-11-19 ' + stamp)
        monkeypatch.setattr(monitor.market_clock, 'now', lambda: bar.minute_start + near.MINUTE)
        # Each connection/evaluator is new: throttle and dedup survive restart.
        with db.session(path) as conn:
            previous = conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0]
            state = state_for(hours, bar)
            sent = monitor.check_alerts(conn, cfg, config, state, notifier=Notifier())
            assert sent == int(cooldown == 0 or i in (0, 3))
            assert monitor.check_alerts(conn, cfg, config, state, notifier=Notifier()) == 0
            assert conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == previous + sent
    assert len(queued) == expected
    # An opposite direction uses its own cooldown key.
    hours, line = market(1)
    with db.session(path) as conn:
        assert monitor.check_alerts(conn, cfg, config,
            state_for(hours, minute(line, '2026-11-19 09:00')), notifier=Notifier()) == 1


@pytest.mark.parametrize('code,mode', [('TMFL6', 'production'), ('TMFR1', 'mock')])
def test_monitor_only_signal_code_non_mock(config, tmp_path, monkeypatch, code, mode):
    hours, line = market()
    bar = minute(line)
    monkeypatch.setattr(monitor.market_clock, 'now', lambda: bar.minute_start + near.MINUTE)
    cfg = monitor.MonitorConfig(code=code, mode=mode, db_path=tmp_path/'market.db')
    with db.session(cfg.db_path) as conn:
        assert monitor.check_alerts(conn, cfg, config, state_for(hours, bar)) == 0
        assert conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 0
