"""Continue a fixed range of Jetson batches after the current scanner releases its lock."""

import argparse
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import re
import subprocess
import sys

from rolling_scanner import atomic_json, atomic_text, log, read_json


def can_resume(root):
    if (root / 'STOP').exists():
        return 'Manual STOP sentinel is present'
    reason_path = root / 'STOP_REASON.txt'
    if reason_path.is_file():
        reason = reason_path.read_text(encoding='utf-8').strip().splitlines()[-1]
        if re.fullmatch(r'v21_batch_\d+: \d+ high-priority uncataloged candidate signals', reason):
            return None
        if re.fullmatch(r'Pilot limit reached \(\d+ batch attempts\)', reason):
            return None
        return reason
    exit_path = root / 'logs/rolling_v21.exit'
    if exit_path.is_file() and exit_path.read_text(encoding='utf-8').split()[-1] != '0':
        return 'Previous scanner exit requires review'
    return None


def continue_increment(root, first, last):
    state_path = root / 'state/scanner_state.json'
    scanner_lock = root / 'logs/rolling_v21.lock'
    log(f'Increment queued for batches {first}–{last}; waiting for the active scanner to finish')
    with scanner_lock.open('ab') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(state_path)
        plan = {'first_batch': first, 'last_batch': last,
                'science_target_count': (last - first + 1) * 100, 'mode': 'continuous'}
        if state['batch'] > last:
            atomic_json(state_path, {**state, 'increment': {**plan, 'outcome': 'complete'}})
            log(f'Increment already complete through batch {last}')
            return 0
        if state['batch'] < first:
            raise RuntimeError(f'Scanner has an uncommitted batch before requested batch {first}')
        reason = can_resume(root)
        if reason:
            atomic_json(state_path, {**state, 'increment': {**plan, 'outcome': 'paused', 'reason': reason}})
            log(f'Increment paused: {reason}')
            return 2
        atomic_json(state_path, {**state, 'increment': {**plan, 'outcome': 'running'}})
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        command = [sys.executable, str(Path(__file__).with_name('rolling_scanner.py')),
                   '--root', str(root), '--mode', 'continuous', '--last-batch', str(last), '--workers', '4']
        log(f'Running batches {state["batch"]}–{last} continuously with the V2.1 breakers enabled')
        with (root / 'logs/rolling_v21.log').open('ab') as output:
            completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                       pass_fds=(lock.fileno(),), check=False)
        atomic_text(root / 'logs/rolling_v21.exit', f'{stamp} {completed.returncode}\n')
        state = read_json(state_path)
        outcome = 'complete' if state['batch'] > last else 'paused'
        atomic_json(state_path, {**state, 'increment': {**plan, 'outcome': outcome}})
        log(f'Increment {outcome}; next batch {state["batch"]}')
        return completed.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--first-batch', type=int, required=True)
    parser.add_argument('--last-batch', type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.first_batch <= args.last_batch:
        parser.error('Expected a nonempty positive range of batches')
    raise SystemExit(continue_increment(args.root, args.first_batch, args.last_batch))
