"""Validate broker open-position lots for explicit adoption; no broker mutations."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json

from external_reconciliation import date_text
from pnl_calendar import value
from position_reconciliation import text


def position_totals(positions, aliases):
    result = defaultdict(int)
    for item in positions:
        action = text(value(item, 'direction'))
        quantity = value(item, 'quantity')
        if (action not in {'Buy', 'Sell'} or isinstance(quantity, bool)
                or int(quantity) != float(quantity) or int(quantity) < 0):
            raise ValueError('券商部位方向或口數無效')
        code = str(value(item, 'code'))
        result[aliases.get(code, code), action] += int(quantity)
    return {pair: qty for pair, qty in result.items() if qty}


def plan_lots(details, code, action, quantity, account_key, aliases):
    groups = defaultdict(list)
    for item in details:
        raw_code = str(value(item, 'code'))
        qty, entered = value(item, 'quantity'), value(item, 'entry_quantity')
        price = Decimal(str(value(item, 'price')))
        day, ordno = date_text(value(item, 'date')), str(value(item, 'dseq') or '').strip()
        if (aliases.get(raw_code, raw_code) != code or text(value(item, 'direction')) != action
                or text(value(item, 'currency')) != 'TWD' or not ordno or set(ordno) == {'0'}
                or isinstance(qty, bool) or isinstance(entered, bool)
                or int(qty) != float(qty) or int(entered) != float(entered)
                or int(qty) <= 0 or int(entered) != int(qty)
                or not price.is_finite() or price <= 0):
            raise ValueError('外部部位明細不完整或含部分平倉，無法接管')
        groups[day, ordno].append(dict(date=day, dseq=ordno, code=code, direction=action,
            quantity=int(qty), entry_quantity=int(entered), price=float(price), currency='TWD'))
    if not groups or sum(row['quantity'] for rows in groups.values() for row in rows) != quantity:
        raise ValueError('外部部位明細口數與指定接管口數不符')
    lots = []
    for (day, ordno), records in sorted(groups.items()):
        records = sorted(records, key=lambda row: (row['price'], row['quantity']))
        qty = sum(row['quantity'] for row in records)
        key = hashlib.sha256(json.dumps([account_key, code, action, day, ordno]).encode()).hexdigest()
        lots.append(dict(evidence_key=key, entry_date=day, ordno=ordno, quantity=qty,
            price=sum(row['price'] * row['quantity'] for row in records) / qty, details=records))
    return lots
