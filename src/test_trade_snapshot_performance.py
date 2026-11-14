"""Keep snapshot work bounded by market revisions without changing geometry."""
import json

import pandas as pd
import pytest

import db
import trendline as tl
from test_independent_trade import rig

pytestmark = pytest.mark.integration


def test_snapshot_renders_orders_once_then_reads_current_quotes(rig, monkeypatch):
    r = rig
    r.runtime.step()
    calls = []
    def slow_orders():
        calls.append(True)
        # Simulate quotes arriving in the unified DB during expensive rendering.
        r.update('2026-10-30 09:02:28', tick=20123)
        return []
    monkeypatch.setattr(r.service, 'orders', slow_orders)
    snapshot = r.service.snapshot()
    assert len(calls) == 1
    status = snapshot['status']
    assert status['orders'] == snapshot['orders'] == []
    assert status['last_quote'] == status['market_quotes']['TMF209912']['price'] == 20123
    assert status['market_quotes']['TMF209912']['fresh']
    assert status['quote_contracts']['TMF_MOCK']['fresh']
    assert status['quote_contracts']['TMF_MOCK']['last_quote_at'] == status['market_quotes']['TMF209912']['at']


@pytest.mark.parametrize('timeframe', ['1m', '5m', '15m', '1h'])
def test_curve_geometry_reuses_full_history_and_reloads_corrections(rig, monkeypatch, timeframe):
    r = rig
    with db.session(r.path) as conn:
        frame = db.load_minute_bars(conn, 'TMFR1')
        source = tl.source_bars(frame, timeframe)
        # Existing fixture covers multiple source buckets for 1m/5m. Add
        # earlier full history for 15m/1h and require all of it to be retained.
        extra = pd.DataFrame([dict(minute_start=stamp,open=19999,high=20001,low=19998,
            close=20000,volume=1,amount=20000,session_id='2026-10-30 day')
            for stamp in pd.date_range('2026-10-30 08:45', '2026-10-30 11:46',freq='min')])
        db.upsert_minute_bars(conn, 'TMFR1', extra)
        frame = db.load_minute_bars(conn, 'TMFR1')
        source = tl.source_bars(frame, timeframe)
        stamps = source['hour_start' if timeframe == '1h' else 'timestamp']
        anchors = [{'time':int(stamp.value//10**9),'price':20000+index*100}
                   for index,stamp in enumerate(stamps.iloc[:3])]
        first = db.insert_trendline(conn,'TMFR1',anchors,kind='curve',timeframe=timeframe)
        second = db.insert_trendline(conn,'TMFR1',anchors,kind='curve',timeframe=timeframe)
    expected = tl.values_at_times(source, anchors, [frame.iloc[-1]['minute_start']], timeframe=timeframe).iloc[0]
    original_load, original_source = db.load_minute_bars, tl.source_bars
    loads, builds = [], []
    def load(*args, **kwargs):
        loads.append(True)
        return original_load(*args, **kwargs)
    def build(*args, **kwargs):
        builds.append(True)
        return original_source(*args, **kwargs)
    monkeypatch.setattr(db,'load_minute_bars',load)
    monkeypatch.setattr(tl,'source_bars',build)
    row = dict(curve_id=first,code='TMFR1')
    assert r.service._curve_value(row) == pytest.approx(expected)
    for _ in range(3):
        assert r.service._curve_value(dict(row,curve_id=second)) == pytest.approx(expected)
    assert len(loads) == len(builds) == 1
    with db.session(r.path) as conn:
        conn.execute("UPDATE minute_bars SET close=close+1 WHERE code='TMFR1' AND minute_start='2026-10-30 11:45:00'")
        conn.commit()
    r.service._curve_value(row)
    assert len(loads) == len(builds) == 2
    # Drawing edits must invalidate values even when price data is unchanged.
    anchors = [{**anchor,'price':anchor['price']+50} for anchor in anchors]
    with db.session(r.path) as conn:
        conn.execute('UPDATE trendlines SET anchors=? WHERE id=?',(json.dumps(anchors),first))
        conn.commit()
    assert r.service._curve_value(row) == pytest.approx(expected+50)
    assert len(loads) == len(builds) == 2
    with db.session(r.path) as conn:
        conn.execute("DELETE FROM minute_bars WHERE code='TMFR1' AND minute_start='2026-10-30 11:45:00'")
        conn.commit()
    r.service._curve_value(row)
    assert len(loads) == len(builds) == 3


def test_closed_geometry_advances_without_a_new_db_revision(rig):
    r = rig
    # A future fixture candle must not be used until it actually closes.
    with db.session(r.path) as conn:
        db.upsert_minute_bars(conn,'TMFR1',pd.DataFrame([dict(minute_start=pd.Timestamp('2026-10-30 09:02'),
            open=20100,high=20100,low=20100,close=20100,volume=1,amount=20100,session_id='2026-10-30 day')]))
    bars, _ = r.service._curve_source('TMFR1','1m',closed=True)
    assert bars.iloc[-1]['minute_start'] == pd.Timestamp('2026-10-30 09:01')
    r.now[0] = pd.Timestamp('2026-10-30 09:03:03')
    bars, _ = r.service._curve_source('TMFR1','1m',closed=True)
    assert bars.iloc[-1]['minute_start'] == pd.Timestamp('2026-10-30 09:02')
