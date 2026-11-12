"""Request the latest price once per second, directly from Shioaji."""
import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd
import shioaji as sj
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code', help='Contract code; defaults to runtime signal_code')
    parser.add_argument('--runtime-config', type=Path, default=ROOT / 'config/runtime.json')
    args = parser.parse_args()
    config = json.loads(args.runtime_config.read_text(encoding='utf-8-sig'))
    mode = config.get('mode', 'mock')
    if mode not in ('simulation', 'production'):
        print('Mock mode: no broker login. Select simulation/production in runtime config.')
        return
    code = args.code or config.get('signal_code', 'TMFR1')
    load_dotenv(ROOT / '.env')
    api_key = os.getenv('SHIOAJI_API_KEY')
    secret_key = os.getenv('SHIOAJI_SECRET_KEY')
    if not api_key or not secret_key:
        print('Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in project .env.')
        return 1

    api = sj.Shioaji(simulation=(mode == 'simulation'))
    try:
        print(f'mode={mode} code={code}; one additional broker session. Ctrl+C to stop.', flush=True)
        started = time.perf_counter()
        api.login(api_key=api_key, secret_key=secret_key, subscribe_trade=False)
        print(f'login_ms={(time.perf_counter() - started) * 1000:.1f}', flush=True)
        contract = api.contracts.get(code)
        if contract is None:
            print(f'Contract not found: {code}', flush=True)
            return 1
        while True:
            started = time.perf_counter()
            snapshots = api.snapshots([contract], timeout=5000)
            finished = time.perf_counter()
            now = pd.Timestamp.now(tz='Asia/Taipei')
            prefix = f'{now:%H:%M:%S.%f}'[:-3] + f' {code} api_ms={(finished - started) * 1000:.1f}'
            if snapshots:
                snap = snapshots[0]
                # Shioaji market timestamps encode Taiwan wall-clock time.
                quoted = pd.Timestamp(snap.ts, unit='ns').tz_localize('Asia/Taipei')
                age = (now - quoted).total_seconds() * 1000
                print(f'{prefix} price={snap.close} quote_at={quoted} age_ms={age:.1f}', flush=True)
            else:
                print(f'{prefix} NO_DATA', flush=True)
            # Aim for one request per second; slow replies never cause catch-up bursts.
            time.sleep(max(0, 1 - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
    except Exception as error:
        print(f'Failed: {type(error).__name__}', flush=True)
        return 1
    finally:
        print('logout_started', flush=True)
        try:
            result = api.logout()
            print('logout_failed' if result is False else 'logout_succeeded', flush=True)
        except Exception as error:
            print(f'logout_unconfirmed: {type(error).__name__}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
