"""Python adaptation of SuperTrend AI (Clustering) [LuxAlgo], © LuxAlgo.

CC BY-NC-SA 4.0: https://creativecommons.org/licenses/by-nc-sa/4.0/
Adapted from the user-supplied Pine v5 script. Defaults and bar update order
are preserved; chart styling is handled separately. Direction uses the trailing
stop state (Pine `os`), not price versus the adaptive moving average.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from supertrend import wilder_rma

DEFAULTS = dict(atr_length=10, min_factor=1.0, max_factor=5.0, step=0.5,
                performance_memory=10.0, cluster='Best', max_iter=1000, max_data=10000)


def confirmed_supertrend_ai(bars: pd.DataFrame, cutoff) -> pd.DataFrame:
    """Full, closed 15m bars; gaps restart the supplied Pine calculation."""
    closed = bars.loc[bars['timestamp'] + pd.Timedelta(minutes=15) <= pd.Timestamp(cutoff)].copy()
    columns = ['supertrend', 'supertrend_direction', 'target_factor', 'performance_index', 'ama']
    for name in columns:
        closed[name] = np.nan
    if closed.empty:
        return closed
    complete = closed['minutes'].eq(15)
    missing = (closed['session_id'].eq(closed['session_id'].shift())
               & closed['timestamp'].diff().ne(pd.Timedelta(minutes=15)))
    groups = ((~complete) | missing).cumsum()
    for _, block in closed.loc[complete].groupby(groups[complete], sort=False):
        closed.loc[block.index, columns] = calculate_supertrend_ai(block)[columns]
    return closed


def _clusters(performance, factors, max_iter):
    centroids = np.percentile(performance, [25, 50, 75])
    previous = None
    for _ in range(max_iter + 1):
        distances = np.abs(performance[:, None] - centroids)
        # Pine array.min ignores na; indexof chooses the first tied minimum.
        assignments = np.argmin(np.where(np.isnan(distances), np.inf, distances), axis=1)
        groups = [performance[assignments == k] for k in range(3)]
        updated = np.array([g.mean() if len(g) else np.nan for g in groups])
        if np.array_equal(updated, centroids) or (previous is not None and np.array_equal(assignments, previous)):
            # Empty clusters remain na in Pine. Identical membership is a fixed
            # point even when na prevents Pine's equality-based early exit.
            break
        previous, centroids = assignments, updated
    return [(factors[assignments == k], groups[k]) for k in range(3)]


def calculate_supertrend_ai(bars: pd.DataFrame, *, atr_length=10, min_factor=1.0,
                            max_factor=5.0, step=0.5, performance_memory=10.0,
                            cluster='Best', max_iter=1000, max_data=10000):
    if not {'high', 'low', 'close'}.issubset(bars.columns):
        raise ValueError('Missing high, low or close')
    if (atr_length < 1 or min_factor < 0 or max_factor < min_factor or step <= 0
            or performance_memory < 2 or cluster not in ('Best', 'Average', 'Worst')
            or max_iter < 0 or max_data < 0):
        raise ValueError('Invalid SuperTrend AI settings')
    result = bars.copy()
    n = len(bars)
    for column in ('supertrend', 'supertrend_direction', 'target_factor', 'performance_index', 'ama'):
        result[column] = np.nan
    if not n:
        return result
    close = bars['close'].to_numpy(dtype=float)
    midpoint = ((bars['high'] + bars['low']) / 2).to_numpy(dtype=float)
    change = np.diff(close, prepend=np.nan)
    tr = np.maximum(bars['high'].to_numpy() - bars['low'].to_numpy(),
                    np.maximum(np.abs(bars['high'].to_numpy() - np.roll(close, 1)),
                               np.abs(bars['low'].to_numpy() - np.roll(close, 1))))
    tr[0] = bars['high'].iloc[0] - bars['low'].iloc[0]
    atr = wilder_rma(pd.Series(tr), atr_length).to_numpy()
    factors = min_factor + np.arange(int((max_factor - min_factor) / step) + 1) * step
    upper = np.full(len(factors), midpoint[0])
    lower = upper.copy()
    output = np.full(len(factors), np.nan)
    performance = np.zeros(len(factors))
    trend = np.zeros(len(factors), dtype=int)
    target = den = ama = np.nan
    adaptive_upper = adaptive_lower = midpoint[0]
    state = 0
    previous_stop = np.nan
    memory_alpha = 2 / (performance_memory + 1)
    ema_alpha = 2 / (int(performance_memory) + 1)
    selected = {'Worst': 0, 'Average': 1, 'Best': 2}[cluster]
    rows = []
    for i in range(n):
        previous_close = close[i - 1] if i else np.nan
        trend = np.where(close[i] > upper, 1, np.where(close[i] < lower, 0, trend))
        up, down = midpoint[i] + atr[i] * factors, midpoint[i] - atr[i] * factors
        upper = np.where(previous_close < upper, np.minimum(up, upper), up)
        lower = np.where(previous_close > lower, np.maximum(down, lower), down)
        diff = np.nan_to_num(np.sign(previous_close - output), nan=0.0)
        performance += memory_alpha * (np.nan_to_num(change[i], nan=0.0) * diff - performance)
        output = np.where(trend == 1, lower, upper)
        if i:
            den = abs(change[i]) if np.isnan(den) else ema_alpha * abs(change[i]) + (1 - ema_alpha) * den
        perf_index = np.nan
        if n - 1 - i <= max_data:
            cluster_factors, cluster_perf = _clusters(performance, factors, max_iter)[selected]
            if len(cluster_factors):
                target = cluster_factors.mean()
            numerator = max(cluster_perf.mean() if len(cluster_perf) else 0.0, 0.0)
            perf_index = numerator / den if den > 0 else np.nan
        up, down = midpoint[i] + atr[i] * target, midpoint[i] - atr[i] * target
        adaptive_upper = min(up, adaptive_upper) if previous_close < adaptive_upper else up
        adaptive_lower = max(down, adaptive_lower) if previous_close > adaptive_lower else down
        state = 1 if close[i] > adaptive_upper else 0 if close[i] < adaptive_lower else state
        stop = adaptive_lower if state else adaptive_upper
        if np.isnan(previous_stop) and not np.isnan(stop):
            ama = stop
        else:
            ama += perf_index * (stop - ama)
        rows.append((stop, (1 if state else -1) if np.isfinite(stop) else np.nan, target, perf_index, ama))
        previous_stop = stop
    result[['supertrend', 'supertrend_direction', 'target_factor', 'performance_index', 'ama']] = rows
    return result
