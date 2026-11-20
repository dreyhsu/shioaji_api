"""Offline opening alerts with real Supertrend, calendar, claims and SQLite."""
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import alert_rules as ar
import db
import market_health as mh
import monitor
import session_bars as sb
import session_open_alerts as oa

pytestmark = pytest.mark.integration


@pytest.fixture
def make_rig(tmp_path):
    connections = []

    def make(*, direction=-1, start='2026-11-18 08:45', code='TMFR1', mode='simulation',
             monitor_minutes=30, cooldown_minutes=5):
        start = pd.Timestamp(start)
        sessions = []
        for day in pd.date_range('2026-11-13', '2026-11-23', freq='D'):
            if day.weekday() < 5:
                sessions.extend([(day + pd.Timedelta(hours=8, minutes=45), day + pd.Timedelta(hours=13, minutes=45)),
                                 (day + pd.Timedelta(hours=15), day + pd.Timedelta(days=1, hours=5))])
        index = next(i for i, pair in enumerate(sessions) if pair[0] == start)
        history = sessions[max(0, index - 3):index]
        stamps = [stamp for left, right in history for stamp in pd.date_range(left, right - oa.MINUTE, freq='min')]
        prices = [100. if direction == -1 else 50. + i * 0.1 for i in range(len(stamps))]
        frame = sb.add_sessions(pd.DataFrame([dict(minute_start=stamp, open=price, high=price + .2,
            low=price - .2, close=price, volume=1, amount=price) for stamp, price in zip(stamps, prices)]))
        path = tmp_path / f'market-{len(connections)}.db'
        conn = db.connect(path)
        db.init_schema(conn)
        connections.append(conn)
        db.upsert_minute_bars(conn, code, frame)
        calendar = mh.SessionCalendar(dict(source='offline reviewed test fixture',
            coverage_start='2026-11-13', coverage_end='2026-11-25',
            sessions=[dict(start=str(left), end=str(right)) for left, right in sessions]))
        config = monitor.MonitorConfig(code=code, db_path=path, mode=mode)
        alerts = ar.AlertConfig(tmp_path / 'absent-alerts.json')
        alerts.data = ar.merge_config({oa.RULE: dict(monitor_minutes=monitor_minutes,
                                                   cooldown_minutes=cooldown_minutes)})
        engine = oa.SessionOpenAlerts(code, mode, started_at=start - pd.Timedelta(seconds=10))
        r = NS(conn=conn, path=path, start=start, previous=sessions[index - 1],
               end=sessions[index][1], calendar=calendar, code=code, config=config, alerts=alerts,
               engine=engine, direction=direction, open=prices[-1] - direction * 10, latest=-1)

        def put(offset, close=None, *, opening=None):
            price = r.open if close is None else close
            opening = r.open if opening is None else opening
            bar = dict(minute_start=start + offset * oa.MINUTE, open=opening,
                       high=max(price, opening) + .2, low=min(price, opening) - .2,
                       close=price, volume=1, amount=price)
            db.upsert_minute_bars(conn, code, sb.add_sessions(pd.DataFrame([bar])))
            r.latest = max(r.latest, offset)

        def step(*, now=None, ready=True, enabled=True, opening_grace=False, claim=None):
            alerts.reload()
            now = pd.Timestamp(now) if now is not None else start + (r.latest + 1) * oa.MINUTE + pd.Timedelta(seconds=3)
            hourly = sb.minute_bars_to_hourly(db.load_minute_bars(conn, code))
            return r.engine.step(conn, hourly, r.calendar, now, ready=ready, enabled=enabled,
                opening_grace=opening_grace,
                rule_config=alerts.rule(oa.RULE),
                claim=claim or (lambda candidate, minute: monitor.claim_rule(
                    conn, config, alerts, candidate, minute, commit=False)))

        def state():
            row = conn.execute('SELECT document FROM session_open_alert_states '
                               'WHERE code=? AND mode=? AND session_start=?',
                               (code, mode, str(start))).fetchone()
            return json.loads(row[0]) if row else None

        r.put, r.step, r.state = put, step, state
        return r

    yield make
    for conn in connections:
        conn.close()


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('start', ['2026-11-18 08:45', '2026-11-18 15:00'])
def test_first_minute_close_and_five_minute_throttle(make_rig, direction, start):
    r = make_rig(direction=direction, start=start)
    r.put(0, r.open + direction)
    jobs = r.step()
    assert len(jobs) == 1
    alert_id, candidate = jobs[0]
    assert candidate.rule == oa.RULE and not candidate.once
    assert ('放空' if direction == -1 else '做多') in candidate.message
    assert 'ATR 10／倍數 3' in candidate.message
    assert ('日盤' if r.start.hour == 8 else '夜盤') in candidate.message
    assert r.state()['context']['direction'] == direction
    assert r.conn.execute('SELECT ok FROM telegram_alerts WHERE id=?', (alert_id,)).fetchone()[0] is None
    r.put(1, r.open - direction)
    r.put(2, r.open + direction)
    assert r.step() == []
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 1
    assert not r.conn.in_transaction


@pytest.mark.parametrize('direction', [-1, 1])
def test_touch_and_intraminute_wick_do_not_trigger_then_close_does(make_rig, direction):
    r = make_rig(direction=direction)
    r.put(0)  # low/high straddle the open, close equals the open
    assert r.step() == []
    r.put(1, r.open - direction)
    assert r.step() == []
    r.put(2, r.open + direction)
    assert len(r.step()) == 1


@pytest.mark.parametrize('gap', [0, -10])
def test_no_opposite_gap_is_ineligible(make_rig, gap):
    r = make_rig()
    r.open = r.open - 10 + gap
    r.put(0, r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'ineligible'


def test_normal_opening_grace_does_not_suppress_first_minute(make_rig):
    r = make_rig()
    assert r.step(now=r.start + pd.Timedelta(seconds=20), ready=False, opening_grace=True) == []
    assert r.state()['reason'] == 'missing_open'
    r.put(0, r.open - 1)
    assert len(r.step()) == 1


@pytest.mark.parametrize('flag', ['ready', 'enabled'])
def test_pause_consumes_crossing_and_resume_does_not_repeat(make_rig, flag):
    r = make_rig()
    r.put(0, r.open - 1)
    assert r.step(**{flag: False}) == []
    assert r.state()['status'] == 'triggered'
    r.put(1, r.open + 1)
    r.put(2, r.open - 1)
    assert r.step() == []
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 0


@pytest.mark.parametrize('flag', ['ready', 'enabled'])
def test_recovery_does_not_deliver_delayed_current_crossing(make_rig, flag):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step(**{flag: False})
    r.put(1, r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'triggered'


def test_restart_consumes_crossing_that_happened_while_down(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    r.put(1, r.open - 1)
    r.engine = oa.SessionOpenAlerts(r.code, r.config.mode,
        started_at=r.start + pd.Timedelta(minutes=2, seconds=1))
    assert r.step() == []  # even though the missed close is less than 60 seconds old
    assert r.state()['status'] == 'triggered'


def test_restart_can_wait_for_a_future_new_crossing(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    r.engine = oa.SessionOpenAlerts(r.code, r.config.mode,
        started_at=r.start + pd.Timedelta(minutes=1, seconds=10))
    r.put(1, r.open - 1)
    assert len(r.step()) == 1


def test_cold_start_reconstructs_earliest_missed_crossing(make_rig):
    r = make_rig()
    for offset, price in enumerate([r.open - 1, r.open + 1, r.open - 1]):
        r.put(offset, price)
    r.engine = oa.SessionOpenAlerts(r.code, r.config.mode,
        started_at=r.start + pd.Timedelta(minutes=3, seconds=1))
    assert r.step() == []
    assert r.state()['trigger_minute'] == str(r.start)


def test_stale_close_not_sent(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    assert r.step(now=r.start + 2 * oa.MINUTE) == []
    assert r.state()['status'] == 'triggered'


@pytest.mark.parametrize('missing', ['open', 'previous_tail', 'previous_middle', 'previous_session'])
def test_missing_boundaries_and_previous_session_do_not_guess(make_rig, missing):
    r = make_rig()
    r.put(0, r.open - 1)
    r.put(1, r.open - 2)
    if missing == 'open':
        r.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start=?', (r.code, str(r.start)))
    elif missing == 'previous_session':
        r.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start>=? AND minute_start<?',
                       (r.code, str(r.previous[0]), str(r.previous[1])))
    else:
        stamp = r.previous[1] - oa.MINUTE if missing == 'previous_tail' else r.previous[0] + oa.MINUTE
        r.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start=?', (r.code, str(stamp)))
    r.conn.commit()
    assert r.step() == []
    assert r.state()['status'] == 'waiting'


def test_complete_close_after_gap_qualifies_and_repair_is_silent(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    r.put(2, r.open - 1)
    assert len(r.step()) == 1  # each complete close stands on its own
    assert r.state()['status'] == 'triggered'
    r.put(1, r.open - 1)  # late broker repair reveals the earlier crossing
    assert r.step() == []
    assert r.state()['trigger_minute'] == str(r.start + 2 * oa.MINUTE)


def test_repaired_opening_is_rebuilt_silently(make_rig):
    r = make_rig()
    r.put(1, r.open - 1)
    assert r.step() == []
    r.put(0, r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'triggered'


def test_opening_context_revision_invalidates_waiting_signal(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    r.put(0, r.open + 1, opening=r.open + 2)
    r.put(1, r.open - 1)
    assert r.step() == []
    assert r.state()['reason'] == 'opening_context_revised'


def test_minute_revision_reconstructs_without_new_notification(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    r.put(0, r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'triggered'


def test_supertrend_flip_cancels_even_if_it_later_flips_back(make_rig, monkeypatch):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    real = r.engine._confirmed_hours

    def hours(hourly, now):
        result = real(hourly, now).copy()
        extra = pd.DataFrame([
            dict(available_at=r.start + 2 * oa.MINUTE, supertrend=90., supertrend_direction=1),
            dict(available_at=r.start + 3 * oa.MINUTE, supertrend=120., supertrend_direction=-1)])
        return pd.concat([result, extra], ignore_index=True).sort_values('available_at')

    monkeypatch.setattr(r.engine, '_confirmed_hours', hours)
    r.put(1, r.open + 1)
    r.put(2, r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'cancelled'


def test_insufficient_supertrend_warmup_waits(make_rig):
    r = make_rig(start='2026-11-18 15:00')
    r.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start<?', (r.code, str(r.previous[0])))
    r.conn.commit()
    r.put(0, r.open - 1)
    assert r.step() == []
    assert r.state()['reason'] == 'opening_supertrend_unavailable'


def test_unverified_calendar_and_closed_session_do_not_alert(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    r.calendar = mh.SessionCalendar()
    assert r.step() == []
    assert r.state() is None


def test_session_expires_at_close(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    assert r.step(now=r.end + oa.MINUTE) == []
    assert r.state()['status'] == 'expired'


def test_repeated_poll_does_not_write_or_recalculate_indicator(make_rig, monkeypatch):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    changes = r.conn.total_changes
    monkeypatch.setattr(oa, 'confirmed_supertrend', lambda *args: pytest.fail('cached hourly ST recomputed'))
    assert r.step() == []
    r.put(1, r.open + 2)
    assert r.step() == []
    assert r.conn.total_changes > changes
    changes = r.conn.total_changes
    r.step()
    assert r.conn.total_changes == changes


def test_state_and_claim_rollback_together_then_retry(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)

    def fail(candidate, minute):
        monitor.claim_rule(r.conn, r.config, r.alerts, candidate, minute, commit=False)
        raise RuntimeError('injected failure before transaction commit')

    with pytest.raises(RuntimeError):
        r.step(claim=fail)
    assert r.state() is None
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 0
    assert len(r.step()) == 1


def test_forming_hour_cannot_change_opening_regime(make_rig):
    r = make_rig()
    r.put(0, r.open + 1000)
    assert r.step() == []
    assert r.state()['context']['direction'] == -1
    assert r.state()['context']['opening_st_at'] == str(r.previous[1])


def test_hourly_close_is_outside_opening_monitor_window(make_rig):
    r = make_rig()
    r.put(0, r.open + 1)
    r.step()
    for offset in range(1, 60):
        r.put(offset, r.open + 1 if offset < 59 else r.open - 1)
    assert r.step() == []
    assert r.state()['status'] == 'expired'
    assert r.state()['reason'] == 'monitoring_window_ended'


def test_weekend_uses_calendar_previous_session(make_rig):
    r = make_rig(start='2026-11-16 08:45')  # Monday, previous night ends Saturday
    r.put(0, r.open - 1)
    assert len(r.step()) == 1
    assert r.state()['context']['previous_end'] == '2026-11-14 05:00:00'


def test_calendar_confirmed_holiday_is_a_normal_break(make_rig):
    r = make_rig(start='2026-11-17 08:45')
    r.calendar.sessions = [(left, right) for left, right in r.calendar.sessions
                           if left.date() != pd.Timestamp('2026-11-16').date()]
    r.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start>=? AND minute_start<?',
                   (r.code, '2026-11-16 08:45:00', '2026-11-17 05:00:00'))
    r.conn.commit()
    r.put(0, r.open - 1)
    assert len(r.step()) == 1
    assert r.state()['context']['previous_end'] == '2026-11-14 05:00:00'


def test_night_signal_after_midnight_is_outside_window(make_rig):
    r = make_rig(start='2026-11-18 15:00')
    r.open = 100.1  # small gap stays below the bearish ST throughout the night
    for offset in range(541):
        r.put(offset, r.open + .1 if offset < 540 else r.open - .1)
    assert r.step() == []
    assert r.state() is None


@pytest.mark.parametrize('changes,expected', [({}, 1), ({'code': 'TMFK6'}, 0),
    ({'code': 'TMFK6', 'signal_code': 'TMFK6'}, 1), ({'rule_enabled': False}, 0),
    ({'master': False}, 0), ({'process': False}, 0), ({'mode': 'mock'}, 0)])
def test_real_monitor_wires_only_signal_code_and_commits_before_enqueue(make_rig, tmp_path, monkeypatch,
                                                                     changes, expected):
    from fetcher_storage import FetcherStore
    code = changes.get('code', 'TMFR1')
    r = make_rig(code=code, mode=changes.get('mode', 'simulation'))
    r.config.signal_code = changes.get('signal_code', 'TMFR1')
    r.config.enable_telegram = changes.get('process', True)
    now, mono = [r.start - pd.Timedelta(seconds=5)], [0.]
    monkeypatch.setattr(monitor.market_clock, 'now', lambda: now[0])
    alerts_path = tmp_path / 'runtime-alerts.json'
    settings = dict(telegram_enabled=changes.get('master', True),
        **{name: dict(enabled=False) for name in ar.DEFAULTS if isinstance(ar.DEFAULTS[name], dict)})
    settings[oa.RULE] = dict(enabled=changes.get('rule_enabled', True))
    alerts_path.write_text(json.dumps(settings), encoding='utf-8')
    r.config.alerts_config = alerts_path
    r.config.fetcher_db = tmp_path / f'acquisition-{code}.db'
    sent = []

    class Notifier:
        def __init__(self, *args, **kwargs):
            pass

        def set_enabled(self, enabled):
            pass

        def submit(self, alert_id, message):
            with db.read_session(r.path) as reader:
                assert reader.execute('SELECT status FROM session_open_alert_states').fetchone()[0] == 'triggered'
                assert reader.execute('SELECT id FROM telegram_alerts WHERE id=?', (alert_id,)).fetchone()
            sent.append(message)
            return True

        def snapshot(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(monitor, 'MonitorNotifier', Notifier)
    store = FetcherStore(r.config.fetcher_db)
    runtime = monitor.MonitorRuntime(r.conn, r.config, calendar=r.calendar, settings=mh.HealthSettings(),
                                    clock=lambda: now[0], monotonic=lambda: mono[0])
    try:
        # A normal initial opening grace is followed by a freshly closed source
        # candle; the feature sees data only after monitor imports it.
        now[0] = r.start + pd.Timedelta(seconds=20)
        runtime.step()
        now[0] = r.start + pd.Timedelta(minutes=1, seconds=3)
        mono[0] += 1
        store.bars(code, r.config.mode, [dict(minute_start=str(r.start), open=r.open,
            high=r.open + 1, low=r.open - 2, close=r.open - 1,
            volume=1, amount=r.open, complete=True)], 'broker', 'offline', 1)
        runtime.step()
        assert len(sent) == expected
        assert mh.read_health(runtime.publisher.path)['state'] == 'healthy'
        if code == r.config.signal_code and r.config.mode != 'mock':
            assert r.state()['status'] == 'triggered'
        else:
            assert r.state() is None
    finally:
        runtime.close()
        store.close()


@pytest.mark.parametrize('transport_ok', [True, False])
def test_opening_delivery_is_background_and_failure_never_replays(make_rig, transport_ok):
    import threading
    from monitor_notify import MonitorNotifier
    from test_monitor_notify import eventually
    r = make_rig()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def send(message):
        calls.append(message)
        entered.set()
        assert release.wait(5)
        return transport_ok

    worker = MonitorNotifier(r.path, r.alerts.path, send=send, logger=lambda message: None)
    try:
        r.put(0, r.open - 1)
        alert_id, candidate = r.step()[0]
        assert monitor.enqueue_claimed_rule(r.conn, r.config, candidate, alert_id, notifier=worker)
        assert entered.wait(3)
        # The sender is blocked, but no transaction is held over network I/O.
        r.conn.execute('BEGIN IMMEDIATE')
        r.conn.rollback()
        r.put(1, r.open + 1)
        r.put(2, r.open - 1)
        assert r.step() == []
        release.set()
        eventually(lambda: worker.snapshot()['sent' if transport_ok else 'failed'] == 1)
        assert r.conn.execute('SELECT ok FROM telegram_alerts').fetchone()[0] == int(transport_ok)
        assert len(calls) == 1
    finally:
        release.set()
        worker.close(timeout=3)


def test_missing_whole_current_hour_cannot_extend_monitoring_window(make_rig):
    r = make_rig()
    r.open = 100.1
    r.put(0, 100.2)
    r.step()
    # An hour disappears; the following two minutes are adjacent to each
    # other but cannot borrow an older Supertrend across the missing hour.
    r.put(61, 100.2)
    r.put(62, 100.0)
    assert r.step() == []
    assert r.state()['status'] == 'expired'


def test_first_minute_can_emit_on_next_session_after_previous_signal(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    assert len(r.step()) == 1
    # Seed the rest of the day without changing its bearish hourly regime.
    for offset in range(300):
        r.put(offset, 100., opening=110. if offset == 0 else 100.)
    new_start = r.start.normalize() + pd.Timedelta(hours=15)
    r.start = new_start
    price = 101.
    db.upsert_minute_bars(r.conn, r.code, sb.add_sessions(pd.DataFrame([
        dict(minute_start=new_start, open=price, high=price + .2, low=99.8,
             close=100., volume=1, amount=100.)])))
    assert len(r.step(now=new_start + pd.Timedelta(minutes=1, seconds=3))) == 1
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 2


def test_session_final_minute_is_outside_opening_window(make_rig):
    r = make_rig()
    r.open = 100.1
    for offset in range(300):
        r.put(offset, r.open + .1 if offset < 299 else r.open - .1)
    assert r.step(now=r.end + pd.Timedelta(seconds=3)) == []


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('start', ['2026-11-18 08:45', '2026-11-18 15:00'])
@pytest.mark.parametrize('window,cooldown', [(30, 5), (10, 3), (7, 2)])
def test_sustained_breakout_uses_configured_window_and_spacing(make_rig, direction, start, window, cooldown):
    r = make_rig(direction=direction, start=start, monitor_minutes=window, cooldown_minutes=cooldown)
    notifications = []
    for offset in range(window + 2):
        r.put(offset, r.open + direction)
        jobs = r.step()
        if jobs:
            notifications.append(offset)
    assert notifications == list(range(0, window, cooldown))
    assert r.state()['status'] == 'expired'
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == len(notifications)


@pytest.mark.parametrize('window', [10, 30])
def test_last_configured_candle_allowed_but_next_is_not(make_rig, window):
    r = make_rig(monitor_minutes=window)
    for offset in range(window - 1):
        r.put(offset, r.open + 1)
        assert r.step() == []
    r.put(window - 1, r.open - 1)
    assert len(r.step(now=r.start + window * oa.MINUTE + pd.Timedelta(seconds=59))) == 1
    r.put(window, r.open - 1)
    assert r.step() == []
    assert r.state()['reason'] == 'monitoring_window_ended'


def test_notification_spacing_uses_elapsed_time_not_only_candle_labels(make_rig):
    r = make_rig(cooldown_minutes=3)
    r.put(0, r.open - 1)
    assert len(r.step(now=r.start + oa.MINUTE + pd.Timedelta(seconds=50))) == 1
    for offset in range(1, 4):
        r.put(offset, r.open - 1)
        assert r.step() == []  # 08:49:03 is less than 3m after 08:46:50
    r.put(4, r.open - 1)
    assert len(r.step()) == 1


def test_restart_preserves_cooldown_and_continues_monitoring(make_rig):
    r = make_rig(cooldown_minutes=3)
    r.put(0, r.open - 1)
    assert len(r.step()) == 1
    r.engine = oa.SessionOpenAlerts(r.code, r.config.mode,
        started_at=r.start + oa.MINUTE + pd.Timedelta(seconds=10))
    for offset in (1, 2):
        r.put(offset, r.open - 1)
        assert r.step() == []
    r.put(3, r.open - 1)
    assert len(r.step()) == 1
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 2


def test_recross_inside_cooldown_does_not_bypass_throttle(make_rig):
    r = make_rig(cooldown_minutes=3)
    for offset, difference in enumerate([-1, 1, -1, 1, -1]):
        r.put(offset, r.open + difference)
        assert len(r.step()) == (1 if offset in (0, 4) else 0)


def test_replay_and_recovery_allow_only_future_closes(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    assert r.step(enabled=False) == []
    r.put(1, r.open - 1)
    assert r.step() == []  # recovery must not replay this close
    r.put(2, r.open - 1)
    assert len(r.step()) == 1


def test_forming_first_minute_cannot_notify(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    assert r.step(now=r.start + pd.Timedelta(seconds=59)) == []
    assert len(r.step()) == 1


def test_upgrade_keeps_cooldown_from_version_one_claim(make_rig):
    r = make_rig(cooldown_minutes=3)
    alert_id = db.record_alert(r.conn, r.code, hour_start=r.start,
        box_type=f'open:v1:{r.config.mode}:{r.start.isoformat()}', box_id=None, visit_id=None,
        kind=oa.RULE, message='legacy opening alert')
    r.conn.execute('UPDATE telegram_alerts SET sent_at=? WHERE id=?',
        (str(r.start + oa.MINUTE + pd.Timedelta(seconds=3)), alert_id))
    r.conn.commit()
    r.put(0, r.open - 1)
    r.engine = oa.SessionOpenAlerts(r.code, r.config.mode,
        started_at=r.start + oa.MINUTE + pd.Timedelta(seconds=10))
    for offset in (1, 2):
        r.put(offset, r.open - 1)
        assert r.step() == []
    r.put(3, r.open - 1)
    assert len(r.step()) == 1
    assert r.conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 2


def test_window_reload_can_shorten_then_extend_without_historical_replay(make_rig):
    r = make_rig()
    for offset in range(11):
        r.put(offset, r.open + 1)
    assert r.step() == []
    assert r.state()['status'] == 'armed'
    r.alerts.path.write_text(json.dumps({oa.RULE: dict(monitor_minutes=10)}), encoding='utf-8')
    assert r.step() == []  # config change applies even in the same clock minute
    assert r.state()['status'] == 'expired'
    r.put(11, r.open - 1)
    r.alerts.path.write_text(json.dumps({oa.RULE: dict(monitor_minutes=20)}), encoding='utf-8')
    assert r.step() == []  # extending the window reconstructs silently
    r.put(12, r.open - 1)
    assert len(r.step()) == 1


def test_cooldown_reload_preserves_last_notification_time(make_rig):
    r = make_rig()
    r.put(0, r.open - 1)
    assert len(r.step()) == 1
    r.alerts.path.write_text(json.dumps({oa.RULE: dict(cooldown_minutes=7)}), encoding='utf-8')
    for offset in range(1, 6):
        r.put(offset, r.open - 1)
        assert r.step() == []  # longer than the original 5-minute spacing
    r.alerts.path.write_text(json.dumps({oa.RULE: dict(cooldown_minutes=2)}), encoding='utf-8')
    r.put(6, r.open - 1)
    assert len(r.step()) == 1
    r.put(7, r.open - 1)
    assert r.step() == []
    r.put(8, r.open - 1)
    assert len(r.step()) == 1
