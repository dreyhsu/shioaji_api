import math

import pandas as pd
from mock_market_data import v_shape_bars
from mock_config import MarketConfig


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
