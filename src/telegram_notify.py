"""Telegram alerts for box entries, hourly reminders and reversal patterns.

The transport follows ``src/telegram_sample.py``: POST to ``sendMessage`` with
``parse_mode=HTML`` and credentials from ``.env``.

Nothing here can fail loudly.  A notification problem must never take down the
monitor that is collecting data, so a missing token or a dead network returns
``False`` and the caller records ``ok = 0`` against the alert row.

Rate limiting lives in the database, not here -- ``db.record_alert`` relies on a
UNIQUE constraint over ``(code, hour_start, box_type, kind)`` to decide whether a
message is owed, so this module only formats and sends what it is handed.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5
REQUEST_TIMEOUT = 10

SUPPORT_BADGE = "\U0001f7e9"  # green square
RESISTANCE_BADGE = "\U0001f7e5"  # red square

STATE_LABEL = {"fresh": "未觸發", "holds": "已支撐", "broken": "已突破"}
SIDE_LABEL = {"support": "支撐箱", "resistance": "阻力箱"}
PATTERN_LABEL = {
    "long_lower_tail": "長下影（錘子）",
    "long_upper_tail": "長上影（射擊之星）",
    "bullish_engulfing": "多頭吞噬",
    "bearish_engulfing": "空頭吞噬",
    "piercing_line": "貫穋線",
    "dark_cloud_cover": "烏雲罩頂",
}


def credentials_present() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def send_message(text: str) -> bool:
    """Send one HTML message; returns whether Telegram accepted it."""
    if not credentials_present():
        print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, SEND_RETRIES + 2):
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.ok:
                return True
            print(f"[Telegram] Error {response.status_code}: {response.text[:200]}")
        except Exception as error:  # noqa: BLE001 - notification is best-effort
            print(f"[Telegram] {type(error).__name__}: {error}")
        if attempt <= SEND_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return False


# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------
def _badge(box_type: str) -> str:
    return SUPPORT_BADGE if box_type == "support" else RESISTANCE_BADGE


def _book_line(book_source: str | None, levels: int | None) -> str:
    if not book_source:
        return "委託簿：<b>未取得</b>"
    if book_source == "bidask_v1":
        return f"委託簿：<b>五檔</b>（{levels} 檔，即時）"
    if book_source == "snapshot":
        return "委託簿：<b>一檔</b>（snapshot 降級模式）"
    return f"委託簿：{book_source}"


def _pattern_names(patterns: list[str] | None) -> str:
    if not patterns:
        return "尚未出現"
    return "、".join(PATTERN_LABEL.get(name, name) for name in patterns)


def format_entry_alert(
    *,
    code: str,
    box_type: str,
    box_id: int,
    box_top: float,
    box_bottom: float,
    box_state: str,
    price: float,
    zone_pct: float,
    minute_start,
    hourly_atr: float | None,
    book_source: str | None = None,
    book_levels: int | None = None,
) -> str:
    direction = "回落進入" if box_type == "support" else "上漲進入"
    atr_text = f"{hourly_atr:,.1f}" if hourly_atr and pd.notna(hourly_atr) else "n/a"
    return (
        f"{_badge(box_type)} <b>{code} 價格{direction}{SIDE_LABEL[box_type]}</b>\n"
        f"\U0001f550 {pd.Timestamp(minute_start):%Y-%m-%d %H:%M} (1分K)\n\n"
        f"現價：<b>{price:,.0f}</b>\n"
        f"箱體 #{box_id}：{box_bottom:,.0f} ~ {box_top:,.0f}"
        f"（高 {box_top - box_bottom:,.0f}）\n"
        f"箱內位置：<b>{zone_pct * 100:.0f}%</b>"
        f"（0%=下緣，100%=上緣）\n"
        f"箱體狀態：{STATE_LABEL.get(box_state, box_state)}\n"
        f"小時 ATR：{atr_text}\n"
        f"{_book_line(book_source, book_levels)}\n\n"
        f"<i>已開始記錄委託簿</i>"
    )


def format_hourly_alert(
    *,
    code: str,
    box_type: str,
    box_id: int,
    box_top: float,
    box_bottom: float,
    box_state: str,
    price: float,
    zone_pct: float,
    hour_start,
    minutes_in_box: int,
    min_zone_pct: float | None,
    max_zone_pct: float | None,
    orderbook_rows: int,
    patterns: list[str] | None,
    hourly_atr: float | None,
) -> str:
    atr_text = f"{hourly_atr:,.1f}" if hourly_atr and pd.notna(hourly_atr) else "n/a"
    span = (
        f"{min_zone_pct * 100:.0f}% ~ {max_zone_pct * 100:.0f}%"
        if min_zone_pct is not None and max_zone_pct is not None
        else "n/a"
    )
    return (
        f"{_badge(box_type)} <b>{code} 仍在{SIDE_LABEL[box_type]}內</b>\n"
        f"\U0001f550 {pd.Timestamp(hour_start):%Y-%m-%d %H:%M} 小時棒\n\n"
        f"現價：<b>{price:,.0f}</b>（箱內 {zone_pct * 100:.0f}%）\n"
        f"箱體 #{box_id}：{box_bottom:,.0f} ~ {box_top:,.0f}"
        f"（{STATE_LABEL.get(box_state, box_state)}）\n"
        f"已停留：<b>{minutes_in_box}</b> 分鐘\n"
        f"走過區間：{span}\n"
        f"小時 ATR：{atr_text}\n"
        f"反轉型態：{_pattern_names(patterns)}\n"
        f"委託簿累積：<b>{orderbook_rows:,}</b> 筆"
    )


def format_reversal_alert(
    *,
    code: str,
    box_type: str,
    box_id: int,
    box_top: float,
    box_bottom: float,
    pattern: str,
    direction: str,
    detail: dict,
    price: float,
    zone_pct: float,
    hour_start,
    minutes_into_bar: int,
    bar_closed: bool,
) -> str:
    arrow = "⬆️ 做多" if direction == "long" else "⬇️ 做空"
    status = "已收棒" if bar_closed else f"未收棒（第 {minutes_into_bar} 分）"
    detail_lines = "\n".join(f"  {key} = {value}" for key, value in detail.items())
    return (
        f"⚡ <b>{code} {SIDE_LABEL[box_type]}出現反轉型態</b>\n"
        f"\U0001f550 {pd.Timestamp(hour_start):%Y-%m-%d %H:%M} 小時棒（{status}）\n\n"
        f"型態：<b>{PATTERN_LABEL.get(pattern, pattern)}</b>  {arrow}\n"
        f"現價：<b>{price:,.0f}</b>（箱內 {zone_pct * 100:.0f}%）\n"
        f"箱體 #{box_id}：{box_bottom:,.0f} ~ {box_top:,.0f}\n\n"
        f"<i>量化細節</i>\n<code>{detail_lines}</code>\n\n"
        f"<i>僅為提示，未下單</i>"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a Telegram message or a format demo.")
    parser.add_argument("--message", type=str, help="send this literal text")
    parser.add_argument(
        "--demo", action="store_true", help="print the three templates without sending"
    )
    args = parser.parse_args()

    if args.demo:
        now = pd.Timestamp.now().floor("min")
        print(
            format_entry_alert(
                code="TMFR1",
                box_type="support",
                box_id=7,
                box_top=49600,
                box_bottom=49400,
                box_state="fresh",
                price=49480,
                zone_pct=0.4,
                minute_start=now,
                hourly_atr=145.3,
                book_source="bidask_v1",
                book_levels=5,
            )
        )
        print("\n" + "-" * 60 + "\n")
        print(
            format_hourly_alert(
                code="TMFR1",
                box_type="support",
                box_id=7,
                box_top=49600,
                box_bottom=49400,
                box_state="holds",
                price=49450,
                zone_pct=0.25,
                hour_start=now.floor("h"),
                minutes_in_box=37,
                min_zone_pct=0.1,
                max_zone_pct=0.62,
                orderbook_rows=11234,
                patterns=["long_lower_tail"],
                hourly_atr=145.3,
            )
        )
        print("\n" + "-" * 60 + "\n")
        print(
            format_reversal_alert(
                code="TMFR1",
                box_type="support",
                box_id=7,
                box_top=49600,
                box_bottom=49400,
                pattern="long_lower_tail",
                direction="long",
                detail={"wick_body_ratio": 4.2, "close_pos": 0.81},
                price=49520,
                zone_pct=0.6,
                hour_start=now.floor("h"),
                minutes_into_bar=24,
                bar_closed=False,
            )
        )
    elif args.message:
        ok = send_message(args.message)
        print("sent" if ok else "failed")
    else:
        parser.error("pass --message or --demo")
