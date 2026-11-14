import json
import subprocess
import sys
from pathlib import Path

import db
from latency_trace import LatencyRecorder, link_order, order_link, trace_scope


def test_local_monotonic_duration_and_taipei_timestamps(tmp_path):
    now = [10.]
    recorder = LatencyRecorder(tmp_path / 'trace.db', 'trade', 'TMFR1', 'simulation',
        clock=lambda: '2026-11-14T01:00:00+00:00', monotonic=lambda: now[0])
    with trace_scope(trace_id='command-1', command_id='command-1'):
        start = recorder.record('started')
        now[0] += .125
        recorder.record('completed', started=start, raw_payload={'secret': 'never-record'}, api_key='never-record')
    recorder.flush()
    with db.read_session(recorder.path) as conn:
        rows = conn.execute('SELECT * FROM latency_events ORDER BY monotonic_ns').fetchall()
    assert rows[0]['occurred_at'] == '2026-11-14 09:00:00.000000+08:00'
    assert rows[1]['elapsed_ms'] == 125
    assert rows[0]['instance'] == rows[1]['instance']
    assert rows[0]['trace_id'] == rows[1]['command_id'] == 'command-1'
    assert 'never-record' not in json.dumps([dict(row) for row in rows])


def test_bounded_buffer_and_failed_storage_never_raise(tmp_path):
    recorder = LatencyRecorder(tmp_path / 'blocked.db', 'trade', 'TMFR1', 'simulation', capacity=2)
    recorder.path.mkdir()
    for _ in range(4):
        recorder.record('sample')
    assert recorder.health()['dropped'] == 2
    recorder.flush()
    assert recorder.health()['error'] == 'OperationalError'
    assert recorder.health()['pending'] == 2
    recorder.path.rmdir()
    recorder.flush()
    assert recorder.health()['pending'] == 0


def test_trace_writer_cannot_bypass_offline_real_database_guard():
    recorder = LatencyRecorder(db.DEFAULT_DB_PATH, 'trade', 'TMFR1', 'simulation')
    recorder.record('must_not_write_production')
    recorder.flush()
    assert recorder.health()['error'] == 'RuntimeError'
    assert recorder.health()['pending'] == 1


def test_order_link_survives_new_process_instance_and_keeps_original_claim(tmp_path):
    path = tmp_path / 'orders.db'
    with db.session(path) as conn:
        with trace_scope(trace_id='one', command_id='one', signal_minute='2026-11-13 09:00:00'):
            link_order(conn, 'local-order')
        with trace_scope(trace_id='two', command_id='two'):
            link_order(conn, 'local-order')
        conn.commit()
    assert order_link(path, 'local-order') == dict(trace_id='one', command_id='one', signal_minute='2026-11-13 09:00:00')
    first = LatencyRecorder(path, 'trade', 'TMFR1', 'simulation')
    second = LatencyRecorder(path, 'trade', 'TMFR1', 'simulation')
    assert first.instance != second.instance


def test_readonly_cli_queries_order_without_broker_import(tmp_path):
    path = tmp_path / 'trace.db'
    with db.session(path) as conn, trace_scope(trace_id='trace-one'):
        link_order(conn, 'order-one')
        conn.commit()
    recorder = LatencyRecorder(path, 'trade', 'TMFR1', 'simulation')
    recorder.record('fill_persisted', trace_id='trace-one', order_id='order-one')
    recorder.flush()
    script = Path(__file__).resolve().parents[1] / 'tools/trace_latency.py'
    reply = subprocess.run([sys.executable, str(script), '--db', str(path), '--order-id', 'order-one'],
                           capture_output=True, text=True, check=True, timeout=10)
    rows = json.loads(reply.stdout)
    assert len(rows) == 1 and rows[0]['event'] == 'fill_persisted'
