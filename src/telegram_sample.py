"""
telegram_notifier.py
Send TW picks status to Telegram.

Usage:
  python telegram_notifier.py               # reads data/picks_status.csv and sends
  python telegram_notifier.py --message "custom text"  # send arbitrary message
"""

import os
import argparse
import requests
import pandas as pd
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
STATUS_CSV = 'data/picks_status.csv'


def send_message(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    }, timeout=10)
    if resp.ok:
        print("[Telegram] Message sent.")
        return True
    print(f"[Telegram] Error {resp.status_code}: {resp.text}")
    return False


def format_picks_message(df: pd.DataFrame) -> str:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [f"<b>📊 台股精選 5 檔即時狀態</b>\n🕐 {now}\n"]
    for _, row in df.iterrows():
        symbol = row['代號']
        name = row.get('名稱', '')
        price = row['現價']
        ma10 = row['MA10']
        vs_ma10 = row['距MA10']
        status = row['狀態']

        if pd.notna(price) and pd.notna(ma10) and pd.notna(vs_ma10):
            ma10_sign = '+' if float(vs_ma10) >= 0 else ''
            change_pct = row.get('當日漲幅')
            day_str = ''
            if pd.notna(change_pct):
                day_sign = '+' if float(change_pct) >= 0 else ''
                day_str = f"  當日漲幅: <b>{day_sign}{change_pct}%</b>"
            new_high = row.get('近三日新高', '—')
            lines.append(
                f"{status} <b>{symbol} {name}</b>\n"
                f"  現價: <b>{price}</b>  MA10: {ma10}  距MA10: {ma10_sign}{vs_ma10}%\n"
                f"  近三日新高: {new_high}{('  ' + day_str) if day_str else ''}"
            )
        else:
            lines.append(f"⚠️ <b>{symbol} {name}</b> — 無法取得資料")

    return '\n'.join(lines)


def format_us_picks_message(us_df: pd.DataFrame) -> str:
    lines = ["\n<b>🇺🇸 美股精選 5 檔（前日收盤）</b>\n"]
    for _, row in us_df.iterrows():
        symbol = row['代號']
        name = row.get('名稱', '')
        price = row['現價']
        ma10 = row['MA10']
        vs_ma10 = row['距MA10']
        status = row['狀態']
        date_str = row.get('日期', '')

        if pd.notna(price) and pd.notna(ma10) and pd.notna(vs_ma10):
            ma10_sign = '+' if float(vs_ma10) >= 0 else ''
            change_pct = row.get('當日漲幅')
            day_str = ''
            if pd.notna(change_pct):
                day_sign = '+' if float(change_pct) >= 0 else ''
                day_str = f"  漲跌: <b>{day_sign}{change_pct}%</b>"
            date_info = f"  ({date_str})" if pd.notna(date_str) and date_str else ''
            lines.append(
                f"{status} <b>{symbol} {name}</b>{date_info}\n"
                f"  收盤: <b>${price}</b>  MA10: {ma10}  距MA10: {ma10_sign}{vs_ma10}%"
                f"{('  ' + day_str) if day_str else ''}"
            )
        else:
            lines.append(f"⚠️ <b>{symbol} {name}</b> — 無法取得資料")

    return '\n'.join(lines)


def send_picks_update(df: pd.DataFrame | None = None, us_df: pd.DataFrame | None = None) -> bool:
    if df is None:
        if not Path(STATUS_CSV).exists():
            print(f"[Telegram] {STATUS_CSV} not found. Run tw_picks_monitor.py first.")
            return False
        df = pd.read_csv(STATUS_CSV, dtype={'代號': str})

    msg = format_picks_message(df)
    if us_df is not None and not us_df.empty:
        msg += format_us_picks_message(us_df)
    return send_message(msg)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--message', type=str, help='Send a custom message instead of picks status')
    args = parser.parse_args()

    if args.message:
        send_message(args.message)
    else:
        send_picks_update()
