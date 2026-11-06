"""Bar versions must track corrections independently of high-frequency quotes."""
import pandas as pd

from fetcher_storage import FetcherStore


def test_bar_versions_track_mutations_but_ignore_quotes_and_identical_replays(tmp_path):
    store = FetcherStore(tmp_path / 'acquisition.db')
    row = dict(minute_start=pd.Timestamp('2026-10-30 09:00'), open=100, high=100,
               low=100, close=100, volume=1, amount=100, complete=True)
    def version(code='TMFR1', mode='simulation'):
        result = store.conn.execute('SELECT revision FROM market_bar_versions WHERE code=? AND mode=?',
                                    (code, mode)).fetchone()
        return result[0] if result else 0
    try:
        store.bars('TMFR1', 'simulation', [row], 'stream', 'first', 1)
        assert version() == 1
        store.bars('TMFR1', 'simulation', [row], 'stream', 'first', 1)
        store.quotes([dict(code='TMFR1', mode='simulation', price=101, volume=1,
                           quote_at='2026-10-30 09:02:03', received_at='2026-10-30 09:02:03',
                           instance='first', generation=1)])
        assert version() == 1
        store.invalidate('TMFR1', 'simulation', [row['minute_start']])
        assert version() == 2
        store.bars('TMFR1', 'simulation', [dict(row, close=102)], 'broker', 'history', 1)
        assert version() == 3
        store.bars('TMFR1', 'production', [row], 'stream', 'other', 1)
        store.bars('TMFK6', 'simulation', [row], 'stream', 'other', 1)
        assert version() == 3 and version(mode='production') == version(code='TMFK6') == 1
        store.conn.execute("UPDATE market_stream_bars SET generation=2 WHERE code='TMFR1' AND mode='simulation' AND source='stream'")
        assert version() == 4
        store.conn.execute("DELETE FROM market_stream_bars WHERE code='TMFR1' AND mode='simulation' AND source='stream'")
        assert version() == 5
        store.conn.commit()
        identity = store.conn.execute('SELECT identity FROM market_storage_identity').fetchone()[0]
        store.close()
        store = FetcherStore(tmp_path / 'acquisition.db')
        assert version() == 5
        assert store.conn.execute('SELECT identity FROM market_storage_identity').fetchone()[0] == identity
    finally:
        store.close()
