"""Subscribe to mini Taiwan index futures ticks directly from Shioaji."""
import argparse
import json
import os
import time
import math
from pathlib import Path
from queue import Empty, Full, Queue

import pandas as pd
import shioaji as sj
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code', default='MXFR1', help='Futures code; default: MXFR1 (mini near-month)')
    parser.add_argument('--runtime-config', type=Path, default=ROOT / 'config/runtime.json')
    parser.add_argument('--seconds', type=float, help='Stop after this many seconds of sampling')
    args = parser.parse_args()
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error('--seconds must be positive')
    config = json.loads(args.runtime_config.read_text(encoding='utf-8-sig'))
    mode = config.get('mode', 'mock')
    if mode not in ('simulation', 'production'):
        print('Mock mode: no broker login. Select simulation/production in runtime config.')
        return
    code = args.code
    load_dotenv(ROOT / '.env')
    api_key = os.getenv('SHIOAJI_API_KEY')
    secret_key = os.getenv('SHIOAJI_SECRET_KEY')
    if not api_key or not secret_key:
        print('Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in project .env.')
        return 1

    api = sj.Shioaji(simulation=(mode == 'simulation'))
    ticks = Queue(maxsize=1000)
    overflowed = False
    subscribed = False

    def on_tick(tick):
        nonlocal overflowed
        # Keep SDK delivery fast; formatting and console I/O run in the main thread.
        received_ns = time.time_ns()
        try:
            ticks.put_nowait((tick, received_ns))
        except Full:
            overflowed = True

    try:
        print(f'mode={mode} code={code}; one additional broker session. Ctrl+C to stop.', flush=True)
        started = time.perf_counter()
        api.login(api_key=api_key, secret_key=secret_key, subscribe_trade=False)
        print(f'login_ms={(time.perf_counter() - started) * 1000:.1f}', flush=True)
        contract = api.contracts.get(code)
        if contract is None:
            print(f'Contract not found: {code}', flush=True)
            return 1
        api.set_on_tick_fop_v1_callback(on_tick)
        api.subscribe(contract, quote_type=sj.QuoteType.Tick)
        subscribed = True
        print(f'Subscribed: {code} target_code={contract.target_code}; waiting for ticks.', flush=True)
        deadline = time.perf_counter() + args.seconds if args.seconds is not None else float('inf')
        # Keep the process alive even when the market has no new trades.
        while time.perf_counter() < deadline:
            if overflowed:
                print('Tick queue overflow: console cannot keep up; stopping.', flush=True)
                return 1
            try:
                tick, received_ns = ticks.get(timeout=max(0, min(0.2, deadline - time.perf_counter())))
            except Empty:
                continue
            received = pd.Timestamp(received_ns, unit='ns', tz='UTC').tz_convert('Asia/Taipei')
            # tick.datetime is already Taiwan market time; attach, never add eight hours.
            quoted = pd.Timestamp(tick.datetime)
            quoted = quoted.tz_localize('Asia/Taipei') if quoted.tzinfo is None else quoted.tz_convert('Asia/Taipei')
            lag_ms = (received - quoted).total_seconds() * 1000
            queue_ms = (time.time_ns() - received_ns) / 1_000_000
            print(f'{received:%H:%M:%S.%f} {code} tick_code={tick.code} price={tick.close} '
                  f'volume={tick.volume} quote_at={quoted} receive_lag_ms={lag_ms:.1f} '
                  f'display_queue_ms={queue_ms:.1f} simtrade={int(tick.simtrade)}', flush=True)
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
    except Exception as error:
        print(f'Failed: {type(error).__name__}', flush=True)
        return 1
    finally:
        if subscribed:
            try:
                api.unsubscribe(contract, quote_type=sj.QuoteType.Tick)
                print('unsubscribed', flush=True)
            except Exception as error:
                print(f'unsubscribe_failed: {type(error).__name__}', flush=True)
        print('logout_started', flush=True)
        try:
            result = api.logout()
            print('logout_failed' if result is False else 'logout_succeeded', flush=True)
        except Exception as error:
            print(f'logout_unconfirmed: {type(error).__name__}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
