from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_rma(values: pd.Series, length: int) -> pd.Series:
    """Wilder RMA：以第一个 length 周期的 SMA 初始化。"""
    if length <= 0:
        raise ValueError("length must be positive")

    values = values.astype(float)
    result = pd.Series(np.nan, index=values.index, dtype=float)

    if len(values) < length:
        return result

    first_valid = values.iloc[:length]
    if first_valid.isna().any():
        return result

    result.iloc[length - 1] = first_valid.mean()

    for i in range(length, len(values)):
        if pd.isna(values.iloc[i]):
            continue

        result.iloc[i] = (
            result.iloc[i - 1] * (length - 1) + values.iloc[i]
        ) / length

    return result


def confirmed_supertrend(bars: pd.DataFrame, cutoff, minutes: int = 60) -> pd.DataFrame:
    """Calculate only complete closed session bars, restarting after data gaps.

    Keep incomplete rows as NaN events so consumers cannot carry a stale regime
    through them. Normal session breaks retain Wilder's running history.
    """
    closed = bars.loc[
        bars['timestamp'] + pd.Timedelta(minutes=minutes) <= pd.Timestamp(cutoff)
    ].copy()
    closed['supertrend'] = float('nan')
    closed['supertrend_direction'] = float('nan')
    closed['available_at'] = closed['timestamp'] + pd.Timedelta(minutes=minutes)
    if closed.empty:
        return closed
    complete = closed['minutes'].eq(minutes)
    missing = (closed['session_id'].eq(closed['session_id'].shift())
               & closed['timestamp'].diff().ne(pd.Timedelta(minutes=minutes)))
    groups = ((~complete) | missing).cumsum()
    for _, block in closed.loc[complete].groupby(groups[complete], sort=False):
        values = calculate_supertrend(block)
        closed.loc[values.index, 'supertrend'] = values['supertrend']
        closed.loc[values.index, 'supertrend_direction'] = values['supertrend_direction'].astype(float)
    return closed


def calculate_supertrend(
    bars: pd.DataFrame,
    atr_length: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    计算标准 SuperTrend。

    输入 bars 必须包含：
        high, low, close

    输出栏位：
        true_range
        atr
        basic_upper
        basic_lower
        final_upper
        final_lower
        supertrend
        supertrend_direction

    方向定义：
         1 = 多头，SuperTrend 位于价格下方
        -1 = 空头，SuperTrend 位于价格上方
    """
    required_columns = {"high", "low", "close"}
    missing = required_columns - set(bars.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    if atr_length <= 0:
        raise ValueError("atr_length must be positive")

    if multiplier <= 0:
        raise ValueError("multiplier must be positive")

    result = bars.copy()

    previous_close = result["close"].shift(1)

    result["true_range"] = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    result["atr"] = wilder_rma(
        result["true_range"],
        atr_length,
    )

    midpoint = (result["high"] + result["low"]) / 2.0

    result["basic_upper"] = (
        midpoint + multiplier * result["atr"]
    )
    result["basic_lower"] = (
        midpoint - multiplier * result["atr"]
    )

    final_upper = np.full(len(result), np.nan)
    final_lower = np.full(len(result), np.nan)
    supertrend = np.full(len(result), np.nan)
    direction = np.full(len(result), np.nan)

    for i in range(len(result)):
        if pd.isna(result["atr"].iloc[i]):
            continue

        # 第一根具备 ATR 的 K 棒：初始化为空头。
        if i == 0 or pd.isna(supertrend[i - 1]):
            final_upper[i] = result["basic_upper"].iloc[i]
            final_lower[i] = result["basic_lower"].iloc[i]
            supertrend[i] = final_upper[i]
            direction[i] = -1
            continue

        previous_bar_close = result["close"].iloc[i - 1]

        # 上轨只能下降；前一根收盘突破旧上轨后才重新计算。
        if (
            result["basic_upper"].iloc[i] < final_upper[i - 1]
            or previous_bar_close > final_upper[i - 1]
        ):
            final_upper[i] = result["basic_upper"].iloc[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # 下轨只能上升；前一根收盘跌破旧下轨后才重新计算。
        if (
            result["basic_lower"].iloc[i] > final_lower[i - 1]
            or previous_bar_close < final_lower[i - 1]
        ):
            final_lower[i] = result["basic_lower"].iloc[i]
        else:
            final_lower[i] = final_lower[i - 1]

        current_close = result["close"].iloc[i]

        if supertrend[i - 1] == final_upper[i - 1]:
            # 原本为空头；收盘突破上轨时翻多。
            if current_close > final_upper[i]:
                supertrend[i] = final_lower[i]
                direction[i] = 1
            else:
                supertrend[i] = final_upper[i]
                direction[i] = -1
        else:
            # 原本为多头；收盘跌破下轨时翻空。
            if current_close < final_lower[i]:
                supertrend[i] = final_upper[i]
                direction[i] = -1
            else:
                supertrend[i] = final_lower[i]
                direction[i] = 1

    result["final_upper"] = final_upper
    result["final_lower"] = final_lower
    result["supertrend"] = supertrend
    result["supertrend_direction"] = pd.Series(
        direction,
        index=result.index,
        dtype="Float64",
    )

    result["bullish_flip"] = (
        result["supertrend_direction"].eq(1)
        & result["supertrend_direction"].shift(1).eq(-1)
    )
    result["bearish_flip"] = (
        result["supertrend_direction"].eq(-1)
        & result["supertrend_direction"].shift(1).eq(1)
    )

    return result

