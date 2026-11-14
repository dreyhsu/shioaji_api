"""Read local timing records without importing trading or connecting to a broker."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('data/tmf.db'))
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--trace-id')
    selection.add_argument('--order-id')
    selection.add_argument('--command-id')
    selection.add_argument('--signal-minute', help='Taipei minute-open timestamp; also specify --code')
    selection.add_argument('--snapshot-id')
    parser.add_argument('--code', default='TMFR1')
    parser.add_argument('--fetcher-db', type=Path, help='Include acquisition timing for --signal-minute')
    args = parser.parse_args()
    output = []
    for path in [args.db, *([args.fetcher_db] if args.fetcher_db else [])]:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='latency_events'").fetchone():
                continue  # old fetcher versions have no timing evidence
            trace_id = args.trace_id or args.command_id
            if args.order_id:
                row = conn.execute('SELECT trace_id FROM latency_order_links WHERE order_id=?', (args.order_id,)).fetchone()
                trace_id = row[0] if row else None
            if args.signal_minute:
                # Signal checks/orders use signal_minute; market stages use minute_start.
                clause = "code=? AND (json_extract(document,'$.minute_start')=? OR json_extract(document,'$.signal_minute')=?)"
                params = [args.code, args.signal_minute, args.signal_minute]
            elif args.snapshot_id:
                clause, params = "json_extract(document,'$.snapshot_id')=?", [args.snapshot_id]
            else:
                clause, params = 'trace_id=?', [trace_id]
                if args.order_id:
                    clause, params = '(trace_id=? OR order_id=?)', [trace_id, args.order_id]
            for row in conn.execute(f'SELECT * FROM latency_events WHERE {clause} ORDER BY occurred_at,id LIMIT 1000', params):
                item = dict(row)
                item['document'] = json.loads(item['document'])
                item['database'] = str(path)
                output.append(item)
    print(json.dumps(sorted(output, key=lambda row: (row['occurred_at'], row['id'])), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
