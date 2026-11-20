"""Durable, close-confirmed opening-price alerts from unified data only.

No broker/network I/O. State and alert claims commit together; callers enqueue
the returned jobs only after that transaction completes. Historical replay is
used to consume missed signals, never to deliver them.
"""
from __future__ import annotations

import hashlib
import json
import math

import pandas as pd

from alert_rules import Candidate, DEFAULTS
import db
from supertrend import confirmed_supertrend

RULE = 'session_open_supertrend'
VERSION = 2
MINUTE = pd.Timedelta(minutes=1)
ACTIVE = {'waiting', 'armed', 'triggered'}


def _text(stamp):
    return str(pd.Timestamp(stamp))


def _digest(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes()).hexdigest()


def _finite(values):
    try:
        return all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def _valid_prices(frame):
    return not frame.empty and _finite(frame[['open', 'high', 'low', 'close']].to_numpy().ravel())


def _bars(conn, code, start, end):
    frame = pd.read_sql_query(
        'SELECT minute_start, open, high, low, close FROM minute_bars '
        'WHERE code=? AND minute_start>=? AND minute_start<? ORDER BY minute_start',
        conn, params=(code, _text(start), _text(end)))
    frame['minute_start'] = pd.to_datetime(frame['minute_start'])
    return frame


def _prefix(bars, hours, start, cursor):
    """Bounded to the current session, including ST changes already observed."""
    return (_digest(bars[bars.minute_start <= cursor]),
            _digest(hours[(hours.available_at > start) & (hours.available_at <= cursor + MINUTE)]
                    [['available_at', 'supertrend', 'supertrend_direction']]))


def _message(code, start, state, minute, close):
    short = state['direction'] == -1
    side, trend, gap, crossing, position, action = (
        ('空', '空頭', '向上', '跌破', '低於', '放空') if short else
        ('多', '多頭', '向下', '漲破', '高於', '做多'))
    session = '日盤' if start.hour == 8 else '夜盤'
    return (f'{code}｜{session}開盤價{crossing}・偏{side}觀察\n'
            f'時段：{start:%Y-%m-%d %H:%M}（Asia/Taipei）\n'
            f'1h Supertrend：{trend}（ATR 10／倍數 3，已收定）\n'
            f'開盤趨勢依據：{pd.Timestamp(state["opening_st_at"]):%Y-%m-%d %H:%M}\n'
            f'上一時段末根 1m 收盤：{state["previous_close"]:,.2f}\n'
            f'本時段開盤：{state["open"]:,.2f}（{gap}跳空 '
            f'{state["open"] - state["previous_close"]:+,.2f}）\n'
            f'確認 1m 收盤：{close:,.2f}，{position}開盤價\n'
            f'確認時間：{minute + MINUTE:%Y-%m-%d %H:%M}（Asia/Taipei）\n'
            f'符合開盤反向跳空後順勢穿越條件，可留意{action}機會。')


class SessionOpenAlerts:
    def __init__(self, code, mode, *, started_at):
        self.code, self.mode = code, mode
        self.not_before = pd.Timestamp(started_at)
        self._was_live = None
        self._last_key = None
        self._hour_key = None
        self._hours = None

    def suspend(self, now):
        """A failed acquisition/validation cycle cannot later replay its alerts."""
        self.not_before = max(self.not_before, pd.Timestamp(now))
        self._was_live = False
        self._last_key = None

    def _confirmed_hours(self, hourly, now):
        if hourly.empty:
            return pd.DataFrame(columns=['available_at', 'supertrend', 'supertrend_direction'])
        closed = hourly.loc[pd.to_datetime(hourly['timestamp']) + pd.Timedelta(hours=1) <= now]
        key = _digest(closed[['timestamp', 'session_id', 'high', 'low', 'close', 'minutes']])
        if key != self._hour_key:
            self._hours = confirmed_supertrend(closed, now).sort_values('available_at')
            self._hour_key = key
        return self._hours

    def _load(self, conn, start):
        row = conn.execute('SELECT document FROM session_open_alert_states '
                           'WHERE mode=? AND code=? AND session_start=? AND rule_version=?',
                           (self.mode, self.code, _text(start), VERSION)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, conn, start, state, now):
        conn.execute('INSERT INTO session_open_alert_states VALUES (?,?,?,?,?,?,?) '
                     'ON CONFLICT(mode,code,session_start,rule_version) DO UPDATE SET '
                     'status=excluded.status, document=excluded.document, updated_at=excluded.updated_at',
                     (self.mode, self.code, _text(start), VERSION, state['status'],
                      json.dumps(state, sort_keys=True, ensure_ascii=False), _text(now)))

    def _expire(self, conn, now, monitor_window):
        rows = conn.execute('SELECT session_start, document FROM session_open_alert_states '
                            "WHERE mode=? AND code=? AND rule_version=? AND status IN ('armed','waiting','triggered')",
                            (self.mode, self.code, VERSION)).fetchall()
        for row in rows:
            state = json.loads(row['document'])
            deadline = min(pd.Timestamp(state['session_end']),
                           pd.Timestamp(row['session_start']) + monitor_window)
            if deadline + MINUTE <= now:
                state.update(status='expired', reason='monitoring_window_ended')
                self._save(conn, pd.Timestamp(row['session_start']), state, now)

    def _context(self, conn, bars, hours, previous, start):
        if bars.empty or bars.iloc[0].minute_start != start or not _valid_prices(bars.head(1)):
            return None, 'missing_open'
        if previous is None:
            return None, 'previous_session_unknown'
        previous_start, previous_end = previous
        prior = _bars(conn, self.code, previous_start, previous_end)
        expected = pd.date_range(previous_start, previous_end - MINUTE, freq='min')
        if not pd.DatetimeIndex(prior.minute_start).equals(expected) or not _valid_prices(prior):
            return None, 'previous_session_incomplete'
        opening_hours = hours[hours.available_at <= start]
        if opening_hours.empty:
            return None, 'opening_supertrend_unavailable'
        reading = opening_hours.iloc[-1]
        if (reading.available_at != previous_end or pd.isna(reading.supertrend)
                or not math.isfinite(float(reading.supertrend))
                or pd.isna(reading.supertrend_direction) or reading.supertrend_direction not in (-1, 1)):
            return None, 'opening_supertrend_unavailable'
        return dict(open=float(bars.iloc[0].open), previous_close=float(prior.iloc[-1].close),
                    previous_start=_text(previous_start), previous_end=_text(previous_end),
                    direction=int(reading.supertrend_direction),
                    opening_st=float(reading.supertrend), opening_st_at=_text(reading.available_at)), None

    def step(self, conn, hourly, calendar, now, *, ready, enabled, claim, opening_grace=False,
             rule_config=None):
        """Return committed (alert_id, Candidate) pairs; claim must not commit.

        Only new minutes run through the transition loop. A bounded session
        fingerprint detects corrections and silently reconstructs the prefix.
        A recovered/reenabled cycle never delivers a past crossing.
        """
        now = pd.Timestamp(now)
        settings = {**DEFAULTS[RULE], **(rule_config or {})}
        monitor_window = pd.Timedelta(minutes=settings['monitor_minutes'])
        notify_interval = pd.Timedelta(minutes=settings['cooldown_minutes'])
        live = ready and enabled
        observing = (ready or opening_grace) and enabled
        if self._was_live is False and observing:
            self.not_before = max(self.not_before, now)
        self._was_live = observing
        revision = db.chart_versions(conn, self.code)[0]
        key = (revision, now.floor('min'), id(calendar), ready, enabled, monitor_window, notify_interval)
        if key == self._last_key:
            return []
        jobs = self._step(conn, hourly, calendar, now, revision, live, claim, monitor_window, notify_interval)
        self._last_key = key
        return jobs

    def _step(self, conn, hourly, calendar, now, revision, live, claim, monitor_window, notify_interval):
        with conn:
            self._expire(conn, now, monitor_window)
        if not calendar.covered(now):
            self.suspend(now)
            return []
        active = next(((i, start, end) for i, (start, end) in enumerate(calendar.sessions)
                       if start <= now < end), None)
        if active is None:
            # Give the final minute its normal synchronization window after
            # the session ends; it is still a close inside that session.
            active = next(((i, start, end) for i, (start, end) in enumerate(calendar.sessions)
                           if end <= now < end + MINUTE), None)
        if active is None:
            return []
        index, start, end = active
        if (start.hour, start.minute) not in {(8, 45), (15, 0)}:
            return []
        saved = self._load(conn, start)
        deadline = min(end, start + monitor_window)
        reopened = bool(saved and saved['status'] == 'expired'
                        and saved.get('reason') == 'monitoring_window_ended'
                        and now < deadline + MINUTE)
        if saved and saved['status'] not in ACTIVE and not reopened:
            return []
        # The last monitored candle keeps the usual <60s synchronization window.
        if now >= deadline + MINUTE:
            return []
        bars = _bars(conn, self.code, start, min(deadline, now.floor('min')))
        hours = self._confirmed_hours(hourly, now)
        context, error = self._context(conn, bars, hours,
                                       calendar.sessions[index - 1] if index else None, start)
        state = dict(saved) if saved else dict(status='waiting', session_end=_text(end))
        before = json.dumps(state, sort_keys=True)
        if reopened:
            state['status'] = ('triggered' if state.get('trigger_minute') else
                               'armed' if state.get('context') else 'waiting')
        if saved is None:
            # A live upgrade must also respect an alert claimed by version 1.
            legacy = conn.execute(
                'SELECT MAX(sent_at) FROM telegram_alerts WHERE code=? AND kind=? AND box_type=?',
                (self.code, RULE, f'open:v1:{self.mode}:{start.isoformat()}')).fetchone()[0]
            if legacy is not None:
                state['last_notified_at'] = legacy
        pending = []
        if context is None:
            state.update(status='invalid' if state['status'] in {'armed', 'triggered'} else 'waiting', reason=error)
        elif saved and saved.get('context') and context != saved['context']:
            state.update(status='invalid', reason='opening_context_revised')
        elif not ((context['direction'] == -1 and context['open'] > context['previous_close'])
                  or (context['direction'] == 1 and context['open'] < context['previous_close'])):
            state.update(status='ineligible', reason='no_opposite_opening_gap', context=context)
        else:
            # Missing data repaired after observation, or an already-processed
            # minute/hour revised: reconstruct silently rather than create a
            # new "first" signal from a historical correction.
            replay = reopened or bool(saved and saved['status'] == 'waiting' and not (
                saved.get('reason') == 'missing_open' and now < start + 2 * MINUTE))
            cursor = pd.Timestamp(state['cursor']) if state.get('cursor') else None
            if cursor is not None and list(_prefix(bars, hours, start, cursor)) != state['prefix']:
                replay = True
            if replay:
                cursor = None
            state.update(context=context, status='triggered' if state.get('trigger_minute') else 'armed',
                         reason=None, source_revision=revision)
            if cursor is None:
                state.pop('cursor', None)
            new = bars if cursor is None else bars[bars.minute_start > cursor]
            timeline = hours[['available_at', 'supertrend_direction']].copy()
            timeline['st_available_at'] = timeline.available_at
            if not new.empty:
                values = new.copy()
                values['available_at'] = values.minute_start + MINUTE
                values = pd.merge_asof(values, timeline, on='available_at', direction='backward')
                for bar in values.itertuples(index=False):
                    minute = bar.minute_start
                    expected_hour_end = start + ((bar.available_at - start) // pd.Timedelta(hours=1)) * pd.Timedelta(hours=1)
                    if expected_hour_end > start and bar.st_available_at != expected_hour_end:
                        state.update(status='cancelled', reason='supertrend_unavailable', cursor=_text(minute))
                        break
                    if (pd.isna(bar.supertrend_direction)
                            or int(bar.supertrend_direction) != context['direction']):
                        state.update(status='cancelled', reason='supertrend_changed', cursor=_text(minute))
                        break
                    valid = _finite((bar.open, bar.high, bar.low, bar.close))
                    opening = context['open']
                    # Every complete close on the breakout side qualifies;
                    # wicks, touches and forming candles never qualify.
                    qualifies = valid and (bar.close < opening if context['direction'] == -1 else
                                           bar.close > opening)
                    state.update(cursor=_text(minute))
                    cursor = minute
                    if qualifies:
                        last_notified = state.get('last_notified_at')
                        notify = (live and not replay and self.not_before < bar.available_at
                                  and bar.available_at <= now < bar.available_at + MINUTE
                                  and (last_notified is None or
                                       now >= pd.Timestamp(last_notified) + notify_interval))
                        state.setdefault('trigger_minute', _text(minute))
                        state.setdefault('trigger_close', float(bar.close))
                        state.update(status='triggered',
                                     reason='live_close' if notify else 'historical_paused_or_throttled_close')
                        if notify:
                            candidate = Candidate(
                                RULE, f'open:v{VERSION}:{self.mode}:{start.isoformat()}', False,
                                _message(self.code, start, context, minute, float(bar.close)))
                            pending = [(candidate, minute)]
            if state.get('cursor'):
                state['prefix'] = list(_prefix(bars, hours, start, pd.Timestamp(state['cursor'])))
        # All calculation is complete before taking SQLite's writer lock.
        committed = []
        if json.dumps(state, sort_keys=True) != before or saved is None:
            with conn:
                for candidate, minute in pending:
                    alert_id = claim(candidate, minute)
                    if alert_id is not None:
                        state['last_notified_at'] = _text(now)
                        committed.append((alert_id, candidate))
                self._save(conn, start, state, now)
        return committed
