"""Telegram alerts for the box and trendline rules in ``alert_rules``.

The transport follows ``src/telegram_sample.py``: POST to ``sendMessage`` with
``parse_mode=HTML`` and credentials from ``.env``.

Nothing here can fail loudly.  A notification problem must never take down the
monitor that is collecting data, so a missing token or a dead network returns
``False`` and the caller records ``ok = 0`` against the alert row.

Rate limiting lives in ``monitor.send_rule`` (cooldown and once-per-line, both
read back from the alert rows in the database), so this module only formats and
sends what it is handed.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

import market_clock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5
REQUEST_TIMEOUT = 10

SUPPORT_BADGE = "\U0001f7e9"  # green square
RESISTANCE_BADGE = "\U0001f7e5"  # red square

SIDE_LABEL = {"support": "支撐箱", "resistance": "阻力箱"}


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


def _stamp(minute_start) -> str:
    return f"\U0001f550 {pd.Timestamp(minute_start):%Y-%m-%d %H:%M} 1 分 K 收盤"


FOOTER = "<i>僅為提示，未下單</i>"


def format_zone_alert(
    *,
    code: str,
    box_type: str,
    box_id: int,
    box_top: float,
    box_bottom: float,
    price: float,
    zone_pct: float,
    minute_start,
) -> str:
    """Close in the bottom third of support / top third of resistance."""
    where = "下緣 1/3" if box_type == "support" else "上緣 1/3"
    return (
        f"{_badge(box_type)} <b>{code} 收盤進入{SIDE_LABEL[box_type]}{where}</b>\n"
        f"{_stamp(minute_start)}\n\n"
        f"價格：<b>{price:,.0f}</b>（箱內位置 {zone_pct * 100:.0f}%）\n"
        f"箱體 #{box_id}：{box_bottom:,.0f} – {box_top:,.0f}\n\n"
        f"{FOOTER}"
    )


def format_box_exit_alert(
    *,
    code: str,
    box_type: str,
    box_id: int,
    box_top: float,
    box_bottom: float,
    price: float,
    direction: str,
    minute_start,
) -> str:
    """The previous minute closed inside the box and this one closed outside it."""
    held = (box_type == "support") == (direction == "up")
    if held:
        head = "\U0001f6e1\ufe0f 守住" + ("（向上離開）" if direction == "up" else "（向下離開）")
    else:
        head = "\u2b06\ufe0f 突破" if direction == "up" else "\u2b07\ufe0f 跌破"
    edge = box_top if direction == "up" else box_bottom
    return (
        f"{head} <b>{code} {SIDE_LABEL[box_type]} #{box_id}</b>\n"
        f"{_stamp(minute_start)}\n\n"
        f"價格：<b>{price:,.0f}</b>（{'箱頂' if direction == 'up' else '箱底'} {edge:,.0f}，"
        f"差 {abs(price - edge):,.0f}）\n"
        f"箱體：{box_bottom:,.0f} – {box_top:,.0f}\n\n"
        f"{FOOTER}"
    )


LINE_EVENT = {
    "near": "\U0001f3af 接近",
    "break_below_up": "\U0001f4c9 跌破上升",
    "break_above_down": "\U0001f4c8 突破下降",
}


def format_line_alert(
    *,
    code: str,
    label: str,
    event: str,
    price: float,
    line_value: float,
    slope: float,
    minute_start,
) -> str:
    """Price near a drawn line, or through a rising / falling one."""
    diff = price - line_value
    note = "（此線僅通知一次）" if event != "near" else ""
    return (
        f"{LINE_EVENT[event]} <b>{code} {label}</b>{note}\n"
        f"{_stamp(minute_start)}\n\n"
        f"價格：<b>{price:,.0f}</b>（線值 {line_value:,.0f}，"
        f"{'高' if diff >= 0 else '低'} {abs(diff):,.0f}）\n"
        f"線斜率：{slope:+,.1f} 點 / 小時棒\n\n"
        f"{FOOTER}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a Telegram message or a format demo.")
    parser.add_argument("--message", type=str, help="send this literal text")
    parser.add_argument(
        "--demo", action="store_true", help="print the message templates without sending"
    )
    args = parser.parse_args()

    if args.demo:
        now = market_clock.now().floor("min")
        samples = [
            format_zone_alert(
                code="TMFR1", box_type="support", box_id=7, box_top=49600,
                box_bottom=49400, price=49450, zone_pct=0.25, minute_start=now,
            ),
            format_box_exit_alert(
                code="TMFR1", box_type="resistance", box_id=9, box_top=50200,
                box_bottom=50050, price=50230, direction="up", minute_start=now,
            ),
            format_line_alert(
                code="TMFR1", label="曲線 #3", event="near", price=49620,
                line_value=49600, slope=12.5, minute_start=now,
            ),
            format_line_alert(
                code="TMFR1", label="趨勢線 #4", event="break_below_up", price=49540,
                line_value=49580, slope=8.0, minute_start=now,
            ),
        ]
        print(("\n\n" + "-" * 60 + "\n\n").join(samples))
    elif args.message:
        ok = send_message(args.message)
        print("sent" if ok else "failed")
    else:
        parser.error("pass --message or --demo")
