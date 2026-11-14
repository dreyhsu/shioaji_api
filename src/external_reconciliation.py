"""Plan full external closes from broker realized-P&L evidence, without writes."""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json

import pandas as pd

from pnl_calendar import value
from position_reconciliation import ReconciliationPending, TERMINAL, position_totals, text


def account_key(account):
    identity = [str(value(account, name, '')) for name in ('broker_id', 'account_id')]
    if not identity[1]:
        raise ValueError('missing futures account identity')
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def date_text(raw):
    raw = str(raw)
    return datetime.strptime(raw, '%Y%m%d' if len(raw) == 8 else '%Y-%m-%d').date().isoformat()


def trading_day(stamp, calendar):
    """Use the verified calendar for the business date of a night-session fill."""
    stamp = pd.Timestamp(stamp)
    if stamp.tzinfo:
        stamp = stamp.tz_convert('Asia/Taipei').tz_localize(None)
    for start, end in calendar.sessions:
        if start <= stamp < end:
            if 5 <= start.hour < 15:
                return start.date().isoformat()
            for next_start, _ in calendar.sessions:
                if next_start >= end and 5 <= next_start.hour < 15:
                    return next_start.date().isoformat()
    return None


def entry_fingerprint(fills, calendar):
    if not fills:
        return None
    dates = {date_text(row['filled_at']) if len(str(row['filled_at'])) == 10
             else trading_day(row['filled_at'], calendar) for row in fills}
    if None in dates or len(dates) != 1:
        return None
    prices = Counter()
    for row in fills:
        prices[Decimal(str(row['price']))] += int(row['quantity'])
    return dates.pop(), prices


def normalize_detail(item, aliases):
    record = {name: value(item, name) for name in (
        'date', 'code', 'quantity', 'dseq', 'currency', 'direction', 'entry_date',
        'entry_quantity', 'entry_seqno', 'entry_price', 'cover_price', 'pnl', 'fee', 'tax')}
    record['direction'] = text(record['direction'])
    record['code'] = aliases.get(str(record['code']), str(record['code']))
    for name in ('date', 'entry_date'):
        record[name] = date_text(record[name])
    for name in ('quantity', 'entry_quantity'):
        number = record[name]
        if isinstance(number, bool) or int(number) != float(number) or int(number) <= 0:
            raise ValueError('invalid realized quantity')
        record[name] = int(number)
    for name in ('entry_price', 'cover_price'):
        number = Decimal(str(record[name]))
        if not number.is_finite() or number <= 0:
            raise ValueError('invalid realized price')
        record[name] = float(number)
    if (record['direction'] not in {'Buy', 'Sell'} or record['currency'] != 'TWD'
            or not record['entry_seqno'] or not record['dseq'] or record['entry_date'] > record['date']):
        raise ValueError('incomplete realized identity')
    record['entry_seqno'], record['dseq'] = str(record['entry_seqno']), str(record['dseq'])
    for name in ('pnl', 'fee', 'tax'):
        number = Decimal(str(record[name]))
        if not number.is_finite():
            raise ValueError('invalid realized amount')
        record[name] = float(number)
    return record


def plan_external_closes(rows, history, fills, identities, details, actual, account, calendar, used=()):
    """Require a complete unique original entry and a reconciled whole portfolio.

    Legacy rows without a saved order number require an exact, unique per-price entry
    fingerprint across both local history and the broker's entry-seqno groups.
    Neither a zero net position nor a weighted-average price proves a close.
    """
    groups = defaultdict(list)
    for detail in details:
        key = detail['code'], detail['direction'], detail['entry_date'], detail['entry_seqno']
        groups[key].append(detail)
    fingerprints = {row['id']: entry_fingerprint(fills.get(row['id'], []), calendar) for row in history}
    planned = {row['id']: dict(row) for row in rows}
    repairs, claimed = [], set(used)
    for row in rows:
        pair = row['contract_code'], row['action']
        # Full closes only. An external partial close never guesses allocation.
        if (not row['active'] or actual.get(pair, 0) or row.get('broker_octype') == 'Cover'
                or int(row['filled_quantity']) <= 0 or row['entry_status'] not in TERMINAL
                or int(row['exit_filled_quantity']) != 0
                or row.get('exit_trade_id') and row.get('exit_status') not in {'Failed', 'Rejected', 'Cancelled'}
                or row.get('exit_status') in {'unknown', 'submitting', 'cancelling_entry', 'waiting_entry_cancel'}):
            continue
        fingerprint = fingerprints.get(row['id'])
        if fingerprint is None or sum(fingerprint[1].values()) != int(row['filled_quantity']):
            continue
        identity = identities.get(row['id'])
        if identity and (identity['account_key'] != account or identity['broker_trade_id'] != row['entry_trade_id']):
            continue
        if identity and not identity.get('ordno'):
            identity = None  # migrate older identity rows using the same strict legacy checks
        if not identity:
            equivalent = [other for other in history if other['contract_code'] == pair[0]
                and other['action'] == pair[1] and fingerprints.get(other['id']) == fingerprint]
            if len(equivalent) != 1:
                continue
        matches = []
        for key, records in groups.items():
            if key[:3] != (*pair, fingerprint[0]):
                continue
            # SDK 1.7.7 accounting calls this entry_seqno, but returns the
            # exchange order number (order.ordno), not the platform seqno.
            if identity and (identity.get('ordno') != key[3] or identity['entry_date'] != key[2]):
                continue
            prices = Counter()
            for detail in records:
                prices[Decimal(str(detail['entry_price']))] += detail['quantity']
            if prices != fingerprint[1] or any(record['entry_quantity'] < record['quantity'] for record in records):
                continue
            evidence_key = hashlib.sha256(json.dumps([account, *key]).encode()).hexdigest()
            if evidence_key not in claimed:
                matches.append((evidence_key, records))
        if len(matches) != 1:
            continue
        evidence_key, records = matches[0]
        quantity = sum(record['quantity'] for record in records)
        average = sum(record['quantity'] * record['cover_price'] for record in records) / quantity
        changes = dict(active=0, auto_exit=0, breakeven_enabled=0, curve_armed=0,
            exit_trade_id=None, exit_status='Filled', exit_status_code=None, triggered_at=None,
            exit_filled_quantity=quantity, avg_exit_price=average,
            trigger_reason='external_close', error=None)
        repairs.append(dict(before=dict(row), changes=changes, evidence_key=evidence_key,
            account_key=account, match_method='entry_seqno' if identity else 'unique_entry_fills', details=records))
        planned[row['id']].update(changes)
        claimed.add(evidence_key)
    if repairs and position_totals(planned.values()) != actual:
        raise ReconciliationPending('外部平倉明細尚無法完整核對全部部位，等待券商資料')
    return repairs
