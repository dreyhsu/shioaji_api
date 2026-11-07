import math
import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from mock_market_data import v_shape_bars
from mock_config import MarketConfig, MockConfig, ReplayConfig
import session_bars as sb


def test_scenario_is_deterministic_session_aligned_and_v_shaped():
    settings = MarketConfig()
    bars = v_shape_bars(settings)
    pd.testing.assert_frame_equal(bars, v_shape_bars(settings))
    assert len(bars) == settings.decline_minutes + settings.rebound_minutes + settings.pullback_minutes
    assert bars.minute_start.is_monotonic_increasing and bars.minute_start.is_unique
    assert bars.session_date.dt.dayofweek.max() < 5
    assert bars.close.iloc[0] - bars.close.iloc[settings.decline_minutes] > 2800
    assert bars.close.iloc[settings.decline_minutes + settings.rebound_minutes] - bars.close.iloc[settings.decline_minutes] > 900
    assert bars.close.iloc[-1] < bars.close.iloc[settings.decline_minutes + settings.rebound_minutes]
    assert (bars.high >= bars[["open", "close"]].max(axis=1)).all()
    assert (bars.low <= bars[["open", "close"]].min(axis=1)).all()
    # Preserve the original fixed scenario when using the default settings.
    expected = []
    for index in range(4200):
        value = (27000 - 0.8 * index if index < 3600 else
                 24120 + 2 * (index - 3600) if index < 4080 else
                 25080 - 2 * (index - 4080))
        expected.append(round(value + 0.6 * math.sin(index / 7)))
    assert bars.close.tolist() == expected


def test_custom_scenario_prices_are_continuous_and_have_configured_bars():
    settings = MarketConfig(start_time="2026-10-08 13:43", decline_minutes=3, rebound_minutes=2,
                            pullback_minutes=2, start_price=1000, decline_points_per_minute=10,
                            rebound_points_per_minute=20, pullback_points_per_minute=5,
                            wave_amplitude=0, wick_points=3, volume_base=100, volume_cycle_minutes=2)
    bars = v_shape_bars(settings)
    assert bars.close.tolist() == [1000, 990, 980, 970, 990, 1010, 1005]
    assert bars.volume.tolist() == [100, 101, 100, 101, 100, 101, 100]
    assert (bars.high == bars[["open", "close"]].max(axis=1) + 3).all()
    assert (bars.low == bars[["open", "close"]].min(axis=1) - 3).all()
    assert bars.minute_start.iloc[2] == pd.Timestamp("2026-10-08 15:00")


def test_long_scenario_is_not_limited_to_twelve_calendar_days():
    settings = MarketConfig(decline_minutes=16000, start_price=100000)
    bars = v_shape_bars(settings)
    assert len(bars) == 16600
    assert bars.minute_start.iloc[-1] > pd.Timestamp(settings.start_time) + pd.Timedelta(days=12)
    assert bars.minute_start.is_unique and bars.minute_start.is_monotonic_increasing


@pytest.mark.parametrize('curvature', [-0.9, 0.65, 0.9])
def test_curved_trend_preserves_phase_endpoints_and_price_bounds(curvature):
    settings = MarketConfig(wave_amplitude=0, trend_curvature=curvature)
    bars = v_shape_bars(settings)
    bottom = settings.start_price - settings.decline_minutes * settings.decline_points_per_minute
    peak = bottom + settings.rebound_minutes * settings.rebound_points_per_minute
    assert bars.close.iloc[0] == settings.start_price
    assert bars.close.iloc[settings.decline_minutes] == bottom
    assert bars.close.iloc[settings.decline_minutes + settings.rebound_minutes] == peak
    assert bars.close.iloc[:settings.decline_minutes + 1].is_monotonic_decreasing
    assert bars.close.iloc[settings.decline_minutes:settings.decline_minutes + settings.rebound_minutes + 1].is_monotonic_increasing
    assert bars.close.iloc[settings.decline_minutes + settings.rebound_minutes:].is_monotonic_decreasing
    midpoint = settings.decline_minutes // 2
    linear = settings.start_price - midpoint * settings.decline_points_per_minute
    assert abs(bars.close.iloc[midpoint] - linear) > 400


def demo_config():
    # Fixed test input: a user's chosen replay cursor must not remove the rising
    # phase from this two-direction fitting regression.
    return MockConfig(market=MarketConfig(trend_curvature=.65, wave_amplitude=100, wave_scale_minutes=15),
                      replay=ReplayConfig(start_bar=4020))


def test_configured_replay_shows_curved_swings_in_both_directions_on_15m():
    config = demo_config()
    minutes = v_shape_bars(config.market)
    pd.testing.assert_frame_equal(minutes, v_shape_bars(config.market))
    bars = sb.minute_bars_to_interval(minutes.iloc[:config.warmup_minutes], 15).tail(120)
    turn = minutes.iloc[config.market.decline_minutes].minute_start
    for rising, phase in [(False, bars[bars.timestamp < turn]), (True, bars[bars.timestamp >= turn])]:
        assert len(phase) >= 20
        changes = phase.close.diff().dropna()
        assert (changes > 0).sum() >= 5 and (changes < 0).sum() >= 5
        assert (phase.close.iloc[-1] > phase.close.iloc[0]) == rising
        # Same two-neighbor pivot definition used by the chart fitter, without
        # counting synthetic phase-boundary extrema.
        for field, comparison in [('low', min), ('high', max)]:
            prices = phase[field].tolist()
            pivots = [i for i in range(2, len(prices) - 2)
                      if prices[i] == comparison(prices[i - 2:i + 3])]
            assert len(pivots) >= 3, (rising, field, pivots)
            # Successive extrema follow a changing slope rather than a straight
            # envelope; the overlay curve therefore has something real to fit.
            slopes = [(prices[b] - prices[a]) / (b - a) for a, b in zip(pivots, pivots[1:])]
            assert max(slopes) - min(slopes) > 0.5


def test_chart_auto_curve_can_fit_both_configured_15m_trends():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node required to verify the chart fitter against Mock candles')
    config = demo_config()
    minutes = v_shape_bars(config.market)
    bars = sb.minute_bars_to_interval(minutes.iloc[:config.warmup_minutes], 15).tail(120)
    turn = minutes.iloc[config.market.decline_minutes].minute_start
    phases = []
    for phase in (bars[bars.timestamp < turn], bars[bars.timestamp >= turn]):
        phases.append([{'time': int(row.timestamp.value // 10**9),
                        'open': row.open, 'high': row.high, 'low': row.low, 'close': row.close}
                       for row in phase.itertuples(index=False)])
    script = """
const fit = require(process.argv[1]);
const frames = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(frames.map(bars => {
  const result = fit.fit(bars, {i:0,price:Math.min(...bars.map(b=>b.low))-1},
    {i:bars.length-1,price:Math.max(...bars.map(b=>b.high))+1}, '15m', bars.at(-1).time+900);
  const [a,b,c] = result.points;
  return {direction:result.direction,samples:result.samples.length,
    initial:(b.price-a.price)/(b.i-a.i),terminal:fit.slope(result.points),
    bend:(c.price-b.price)/(c.i-b.i)-(b.price-a.price)/(b.i-a.i)};
})));
"""
    result = subprocess.run([node, '-e', script, str(Path(__file__).parent / 'web' / 'auto_curve.js')],
        input=json.dumps(phases), capture_output=True, text=True, check=True)
    fits = json.loads(result.stdout)
    assert [fit['direction'] for fit in fits] == ['down', 'up']
    for sign, fit in zip((-1, 1), fits):
        assert fit['samples'] >= 3
        assert sign * fit['initial'] >= -1e-8
        assert sign * fit['terminal'] >= -1e-8
        assert sign * fit['bend'] >= -1e-8
