"""Bounded, published unified-DB tick consumption for protective exits."""
from contextlib import closing
import json
import math

import pandas as pd

import db
import market_health as mh
import session_bars as sb
import trendline as tl
from trading import TradingService, align_tick, crossed_exit


def read_batch(service, contract_code, since, state, limit):
    """Read only the coherent, monitor-published prefix, then release the reader."""
    if service._manual_order_market_error(contract_code):
        return None
    now = pd.Timestamp(service._now())
    code = service.bar_codes.get(contract_code, contract_code)
    mode = 'simulation' if service.simulation else 'production'
    cursor = state['tick_seq'] if state else 0
    last = max(since, pd.Timestamp(state['quote_at'])) if state and state['quote_at'] else since
    quote = service._quotes[contract_code]
    cutoff = mh.iso(max(since, now - pd.Timedelta(seconds=service.health_settings.tick_stale_seconds)))
    with db.read_session(service.db_path) as conn:
        conn.execute('BEGIN')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='market_risk_ticks'").fetchone()
        head = 2**63 - 1
        if exists and conn.execute("SELECT 1 FROM sqlite_master WHERE name='market_risk_tick_heads'").fetchone():
            published = conn.execute('''SELECT q.quote_at,q.price,q.instance,q.generation,h.seq
                FROM market_latest_quotes q LEFT JOIN market_risk_tick_heads h USING(code,mode)
                WHERE q.code=? AND q.mode=?''', (code, mode)).fetchone()
            if (published is None or pd.Timestamp(published['quote_at']) != quote['at']
                    or published['price'] != quote['price']
                    or (published['instance'], published['generation']) != service._quote_sources.get(contract_code)):
                service._wake.set()
                return None
            # A missing head publishes no tape, rather than an unbounded prefix.
            head = published['seq'] or 0
        if exists:
            old = conn.execute('''SELECT seq FROM market_risk_ticks
                WHERE code=? AND mode=? AND received_at<? AND seq<=?
                ORDER BY received_at DESC,seq DESC LIMIT 1''', (code, mode, cutoff, head)).fetchone()
            cursor = max(cursor, old['seq'] if old else 0)
        ticks = conn.execute('''SELECT * FROM market_risk_ticks WHERE code=? AND mode=? AND seq>? AND seq<=?
            ORDER BY seq LIMIT ?''', (code, mode, cursor, head, limit + 1)).fetchall() if exists else []
    more = len(ticks) > limit
    if more:
        service._wake.set()
    prices = []
    for tick in ticks[:limit]:
        cursor = tick['seq']
        stamp, received = pd.Timestamp(tick['quote_at']), pd.Timestamp(tick['received_at'])
        if ((tick['instance'], tick['generation']) != service._quote_sources.get(contract_code)
                or stamp < since or received < since or stamp < last
                or not all(-5 <= (now - at).total_seconds() <= service.health_settings.tick_stale_seconds
                           for at in (stamp, received))
                or not -5 <= (received - stamp).total_seconds() <= service.health_settings.tick_stale_seconds):
            continue
        price = float(tick['price'])
        if not math.isfinite(price) or price <= 0:
            continue
        last = stamp
        prices.append((price, str(stamp)))
    if not more and quote['at'] >= max(since, last):
        prices.append((float(quote['price']), str(quote['at'])))
        last = quote['at']
    return cursor, str(last), prices, more


def fixed_breakeven(service, row, price):
    if not row.get('breakeven_enabled') or row.get('breakeven_triggered_at'):
        return {}
    direction = 1 if row['action'] == 'Buy' else -1
    settings = service._order_breakeven_settings(row)
    if row.get('avg_fill_price') is None or direction * (price - row['avg_fill_price']) <= settings['breakeven_trigger_points']:
        return {}
    protection = align_tick(row['avg_fill_price'] + direction * settings['breakeven_offset_points'],
                            float(row.get('contract_tick') or 1), direction)
    stop = row.get('stop_price')
    stop = protection if stop is None else max(stop, protection) if direction > 0 else min(stop, protection)
    return dict(stop_price=stop, breakeven_price=protection, levels_customized=1,
                breakeven_triggered_at=mh.iso(service._now()))


def apply_contract(service, row, limit):
    if (not row.get('active') or row.get('triggered_at') or row.get('exit_trade_id') or row.get('exit_status')
            or int(row['filled_quantity']) <= int(row['exit_filled_quantity'])):
        return row
    now = pd.Timestamp(service._now())
    with db.read_session(service.db_path) as conn:
        state = conn.execute('SELECT * FROM managed_profit_lock_ticks WHERE order_id=?', (row['id'],)).fetchone()
        rules = conn.execute('SELECT saved_at FROM managed_tick_risk_rules WHERE order_id=?', (row['id'],)).fetchone()
        hit = conn.execute('SELECT reason,hit_at FROM managed_tick_exit_hits WHERE order_id=?', (row['id'],)).fetchone()
    since = pd.Timestamp(rules['saved_at'] if rules else row.get('created_at') or now)
    lock_since = pd.Timestamp(row.get('profit_lock_started_at') or now)
    if row.get('profit_lock_enabled'):
        since = max(since, lock_since)
    batch = read_batch(service, row['contract_code'], since, state, limit)
    if batch is None:
        return row
    cursor, last, prices, more = batch
    service._profit_lock_backlog[row['id']] = more
    updated = dict(row)
    policy = service._profit_lock_policy()
    lock_hit = state['hit_at'] if state else None
    new_hit = None
    for price, stamp in prices:
        if hit or lock_hit:
            break
        updated.update(fixed_breakeven(service, updated, price))
        if pd.Timestamp(stamp) >= lock_since:
            updated.update(TradingService._profit_lock_updates(service, updated, price, policy=policy))
        reason = crossed_exit(row['action'], price, updated['stop_price'],
                              None if row.get('curve_id') else row.get('target_price'))
        if reason:
            new_hit = reason, stamp
            if row.get('profit_lock_enabled') and updated.get('profit_lock_price') is not None and crossed_exit(
                    row['action'], price, updated['profit_lock_price'], None):
                lock_hit = stamp
            break
    changes = {key: value for key, value in updated.items() if row.get(key) != value}
    if row.get('profit_lock_enabled') and not row.get('profit_lock_started_at'):
        changes['profit_lock_started_at'] = str(lock_since)
    next_state = cursor, lock_hit, last
    previous = tuple(state[key] for key in ('tick_seq', 'hit_at', 'quote_at')) if state else None
    if not changes and next_state == previous and new_hit is None:
        return row
    with closing(db.connect(service.db_path)) as conn, conn:
        risk_changed = int(any(key not in {'profit_lock_peak_points','profit_lock_started_at'} for key in changes))
        if changes:
            conn.execute('UPDATE managed_orders SET ' + ','.join(f'{key}=?' for key in changes)
                         + ',risk_revision=risk_revision+?,updated_at=? WHERE id=?',
                         (*changes.values(),risk_changed,str(now),row['id']))
        conn.execute('INSERT OR REPLACE INTO managed_profit_lock_ticks VALUES (?,?,?,?)', (row['id'], *next_state))
        if new_hit:
            conn.execute('INSERT OR IGNORE INTO managed_tick_exit_hits VALUES (?,?,?)', (row['id'], *new_hit))
    return {**row, **changes, 'risk_revision': row['risk_revision'] + risk_changed, 'updated_at': str(now)} if changes else row


def curve_values(service, curve, stamps):
    """Project actual drawing geometry to tick times; never create price candles."""
    _, source = service._curve_source(service.code, curve['timeframe'])
    anchors = json.loads(curve['anchors'])
    points = tl.anchor_indices(source, anchors, curve['kind'])
    if not points:
        return [None] * len(stamps)
    starts = tl._starts(source)
    duration = pd.Timedelta(minutes=tl.TIMEFRAME_MINUTES[curve['timeframe']])
    # Extend only the time axis after its existing tail, skipping verified breaks.
    # Anchor indices and existing source buckets remain exactly as saved/displayed.
    target = max(map(pd.Timestamp, stamps))
    axes = getattr(service, '_curve_tick_axes', {})
    service._curve_tick_axes = axes
    token = (target.floor('min'), tuple(service._calendar.sessions))
    cached = axes.get(curve['timeframe'])
    reuse = cached is not None and cached[0] == token and cached[1] is source
    extra = []
    for left, right in (() if reuse else service._calendar.sessions):
        if right <= starts[-1] or left > target:
            continue
        fields = sb.session_fields(left)
        if fields is None:
            continue
        anchor = sb.session_anchor(f'{fields[1]:%Y-%m-%d} {fields[0]}')
        first = max(left, starts[-1] + duration)
        first = anchor + ((first - anchor + duration - pd.Timedelta(nanoseconds=1)) // duration) * duration
        if first < right and first <= target:
            extra.extend(pd.date_range(first, min(right - pd.Timedelta(nanoseconds=1), target), freq=duration))
    if extra:
        starts = starts.append(pd.DatetimeIndex(extra)).sort_values().unique()
    if reuse:
        starts = cached[2]
    else:
        axes[curve['timeframe']] = token, source, starts
    values = []
    for stamp in map(pd.Timestamp, stamps):
        index = starts.get_indexer([stamp], method='pad')[0]
        fraction = (stamp - starts[index]) / duration if index >= 0 else -1
        value = tl.value_at(points, index + fraction, curve['kind']) if 0 <= fraction < 1 else None
        values.append(float(value) if value is not None and math.isfinite(value) else None)
    return values


def curve_signal(service, row, limit):
    if service._manual_order_market_error(service.code):
        return 'curve_unavailable'
    with db.read_session(service.db_path) as conn:
        curve = conn.execute("SELECT anchors,timeframe,kind FROM trendlines WHERE id=? AND code=? "
                             "AND kind IN ('line','curve') AND enabled=1", (row['curve_id'], service.code)).fetchone()
        state = conn.execute('SELECT * FROM managed_curve_exit_ticks WHERE order_id=?', (row['id'],)).fetchone()
        rules = conn.execute('SELECT saved_at FROM managed_tick_risk_rules WHERE order_id=?', (row['id'],)).fetchone()
    if curve is None:
        return 'curve_unavailable'
    since = pd.Timestamp(rules['saved_at'] if rules else row.get('created_at') or service._now())
    batch = read_batch(service, service.code, since, state, limit)
    if batch is None:
        return 'curve_unavailable'
    cursor, last, prices, _ = batch
    values = curve_values(service, curve, [stamp for _, stamp in prices]) if prices else []
    hit = next((stamp for (price, stamp), value in zip(prices, values) if value is not None
                and (price <= value if row['action'] == 'Buy' else price >= value)), None)
    next_state = cursor, last
    previous = tuple(state[key] for key in ('tick_seq', 'quote_at')) if state else None
    if next_state != previous or hit:
        with closing(db.connect(service.db_path)) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO managed_curve_exit_ticks VALUES (?,?,?)', (row['id'], *next_state))
            if hit:
                conn.execute('INSERT OR IGNORE INTO managed_tick_exit_hits VALUES (?,?,?)', (row['id'], 'curve', hit))
            if values and all(value is not None for value in values):
                conn.execute("UPDATE managed_orders SET error=NULL WHERE id=? AND error IN (?,?)", (row['id'],
                    '停利曲線或訊號行情暫不可用；曲線停利暫停，固定停損仍監控',
                    '停利曲線或已收盤 1 分 K 資料暫不可用；曲線停利暫停，固定停損仍監控'))
    return 'curve' if hit else 'curve_unavailable' if any(value is None for value in values) else None
