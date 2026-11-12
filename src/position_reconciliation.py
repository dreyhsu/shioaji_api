"""Conservative, broker-evidenced repair of Auto orders confirmed as Cover.

Only an unambiguous full close is allocated automatically. Net zero alone is
never evidence that a local position closed. Planning performs no writes.
"""
import math
from collections import defaultdict

from pnl_calendar import normalized_fills, value

TERMINAL = {'Filled', 'Cancelled', 'Failed', 'Rejected'}


class ReconciliationPending(ValueError):
    pass


def text(value_):
    return str(getattr(value_, 'value', value_))


def position_totals(rows):
    totals = defaultdict(int)
    for row in rows:
        if row['active']:
            totals[row['contract_code'], row['action']] += int(row['filled_quantity']) - int(row['exit_filled_quantity'])
    return {key: quantity for key, quantity in totals.items() if quantity}


def plan_cover_repairs(rows, trades, actual, aliases=None, entry_fills=None):
    aliases = aliases or {}
    entry_fills = entry_fills or {}
    planned = {row['id']: dict(row) for row in rows}
    repairs = []
    for source in sorted(rows, key=lambda row: (row['created_at'], row['id'])):
        if not source['active'] or source.get('broker_octype') != 'Cover':
            continue
        trade = trades.get(str(source['entry_trade_id']))
        if trade is None or text(value(value(trade, 'status'), 'status')) not in TERMINAL:
            raise ReconciliationPending('反向平倉委託尚未結束，暫停交易等待券商核對')
        quantity = int(source['filled_quantity'])
        if quantity <= 0:
            continue
        order = value(trade, 'order')
        code = str(value(value(trade, 'contract'), 'code', ''))
        if (text(value(order, 'octype')) != 'Cover' or text(value(order, 'action')) != source['action']
                or aliases.get(code, code) != source['contract_code']
                or source.get('exit_trade_id') and (int(source['exit_filled_quantity']) > 0
                    or source.get('exit_status') not in {'Failed', 'Rejected', 'Cancelled'})):
            raise ReconciliationPending('反向平倉成交身分或額外出場待核對，暫停交易')
        candidates = [row for row in planned.values()
            if row['active'] and row['id'] != source['id'] and row.get('broker_octype') != 'Cover'
            and row['contract_code'] == source['contract_code'] and row['action'] != source['action']
            and row['created_at'] <= source['created_at']
            and int(row['filled_quantity']) > int(row['exit_filled_quantity'])]
        if len(candidates) != 1:
            raise ReconciliationPending('反向平倉無法唯一對應原部位，暫停交易請人工核對')
        target = candidates[0]
        if (target['entry_status'] not in TERMINAL or int(target['filled_quantity']) != quantity
                or int(target['exit_filled_quantity']) != 0
                or target.get('exit_trade_id') and target.get('exit_status') not in {'Failed', 'Rejected', 'Cancelled'}
                or target.get('exit_status') in {'unknown', 'submitting', 'cancelling_entry', 'waiting_entry_cancel'}):
            raise ReconciliationPending('反向平倉含部分成交或既有出場，暫停交易請人工核對')
        try:
            fills = normalized_fills(trade)
            if not fills and not value(value(trade, 'status'), 'deals', []):
                fills = entry_fills.get(source['id'], [])
        except (ValueError, TypeError, OverflowError, OSError):
            raise ReconciliationPending('反向平倉成交明細不完整，暫停交易等待核對') from None
        if not fills or sum(fill[3] for fill in fills) != quantity:
            raise ReconciliationPending('反向平倉成交明細不完整，暫停交易等待核對')
        try:
            target_trade = trades.get(str(target['entry_trade_id']))
            original_fills = normalized_fills(target_trade) if target_trade is not None else []
            if not original_fills and not value(value(target_trade, 'status'), 'deals', []):
                original_fills = entry_fills.get(target['id'], [])
        except (ValueError, TypeError, OverflowError, OSError):
            original_fills = []
        if (not original_fills or sum(fill[3] for fill in original_fills) != int(target['filled_quantity'])
                or max(fill[1] for fill in original_fills) > min(fill[1] for fill in fills)):
            raise ReconciliationPending('原部位進場成交明細或先後順序待核對，暫停交易')
        average = sum(fill[3] * fill[4] for fill in fills) / quantity
        if not math.isfinite(average):
            raise ReconciliationPending('反向平倉成交價格不完整，暫停交易等待核對')
        source_changes = dict(active=0, auto_exit=0, reconciled_order_id=target['id'], error=None)
        target_changes = dict(active=0, exit_trade_id=str(source['entry_trade_id']), exit_status='Filled',
            exit_status_code='0000', exit_filled_quantity=quantity, avg_exit_price=average,
            triggered_at=min(fill[1] for fill in fills), trigger_reason='broker_cover', error=None)
        repairs.append((dict(source), dict(target), source_changes, target_changes, trade, fills))
        planned[source['id']].update(source_changes)
        planned[target['id']].update(target_changes)
    if repairs and position_totals(planned.values()) != actual:
        raise ReconciliationPending('券商部位仍有其他差異，不套用推定平倉，請人工核對')
    return repairs
