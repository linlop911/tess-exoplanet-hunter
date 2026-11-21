"""Stream Jetson V2.1 report updates to a Windows receiver over the existing SSH connection."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


ROOT = '/home/data/astro_tess'
POLL_SECONDS = 30
LIMIT = 64 * 1024**2
TOP_LEVEL = {
    'STOP_REASON.txt', 'scanner_state.json', 'rolling_v21.log', 'v21_smoke.log',
    'MASTER_HITS.csv', 'MASTER_HITS.json', 'MASTER_HITS.md', 'sync_status.json', 'v21_monitor.json',
}
BATCH_FILES = {
    'batch_status.json', 'batch_summary.md', 'candidates_ranked.csv', 'candidates_ranked.json',
    'high_priority.json', 'retention.json',
}


def allowed(name):
    parts = name.split('/')
    if len(parts) == 1:
        return name in TOP_LEVEL or re.fullmatch(r'HIT_ALERT_v21_batch_\d{5,}\.md', name) is not None
    if len(parts) != 2:
        return False
    folder, filename = parts
    if folder == 'v21_golden':
        return filename == 'golden_report.json' or re.fullmatch(r'TIC\d+_folded\.png', filename) is not None
    return re.fullmatch(r'v21_batch_\d{5,}', folder) is not None and (
        filename in BATCH_FILES or re.fullmatch(r'candidate_\d+\.png', filename) is not None
    )


def sources(root):
    out = root / 'out'
    for relative in ('STOP_REASON.txt', 'state/scanner_state.json', 'logs/rolling_v21.log', 'logs/v21_smoke.log'):
        path = root / relative
        if path.is_file():
            yield path.name, path
    for directory in sorted(out.iterdir()):
        if not directory.is_dir():
            continue
        marker = 'golden_report.json' if directory.name == 'v21_golden' else 'batch_status.json'
        if not (directory / marker).is_file():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: (item.name == marker, item.name)):
            name = f'{directory.name}/{path.name}'
            if path.is_file() and allowed(name):
                yield name, path
    for path in sorted(out.iterdir()):
        if path.is_file() and path.name.startswith(('MASTER_HITS.', 'HIT_ALERT_')) and allowed(path.name):
            yield path.name, path
    monitor = root / 'state/v21_monitor.json'
    if monitor.is_file():
        yield monitor.name, monitor


def status(root):
    import fcntl

    lock = root / 'logs/rolling_v21.lock'
    running = False
    if lock.is_file():
        with lock.open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
    state_path = root / 'state/scanner_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    reason_path = root / 'STOP_REASON.txt'
    reason = reason_path.read_text(encoding='utf-8').strip() if reason_path.is_file() else None
    batch = state.get('batch')
    checkpoints = root / 'out' / f'v21_batch_{batch:05d}' / 'checkpoints' if batch is not None else None
    return {
        'updated_utc': datetime.now(timezone.utc).isoformat(), 'rolling_active': running,
        'next_batch': batch, 'science_targets_committed': len(state.get('scanned_tic_ids', [])),
        'pending_batch_checkpoints': len(list(checkpoints.glob('*.json'))) if checkpoints else 0,
        'last_completed_batch': state.get('last_batch'), 'stop_reason': reason,
    }


def send_frame(stream, name, payload):
    header = {'name': name, 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    stream.write(json.dumps(header, separators=(',', ':')).encode('ascii') + b'\n')
    stream.write(payload)
    stream.flush()


def send(root):
    signatures = {}
    stream = sys.stdout.buffer
    while True:
        for name, path in sources(root):
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            try:
                with path.open('rb') as file:
                    info = os.fstat(file.fileno())
                    identity = (info.st_ino, info.st_size, info.st_mtime_ns)
                    if signatures.get(name) == identity or info.st_size > LIMIT:
                        continue
                    payload = file.read(info.st_size)
                    if len(payload) != info.st_size:
                        continue
            except FileNotFoundError:
                continue
            send_frame(stream, name, payload)
            signatures[name] = identity
        payload = json.dumps(status(root), indent=2, ensure_ascii=False).encode('utf-8') + b'\n'
        send_frame(stream, 'sync_status.json', payload)
        time.sleep(POLL_SECONDS)


def receive_file(stream, folder):
    header = stream.readline(4097)
    if not header:
        raise EOFError('Jetson report stream closed')
    if len(header) > 4096 or not header.endswith(b'\n'):
        raise ValueError('Invalid report frame header')
    record = json.loads(header)
    name, size, checksum = record['name'], record['size'], record['sha256']
    if not allowed(name) or type(size) is not int or not 0 <= size <= LIMIT or not re.fullmatch(r'[0-9a-f]{64}', checksum):
        raise ValueError('Report frame outside the permitted file set')
    destination = folder / name
    if not destination.resolve().is_relative_to(folder):
        raise ValueError('Report path points outside the destination folder')
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.sync-', suffix='.part', delete=False) as file:
            scratch = Path(file.name)
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                block = stream.read(min(remaining, 256 * 1024))
                if not block:
                    raise EOFError('Report frame ended early')
                file.write(block)
                digest.update(block)
                remaining -= len(block)
        if digest.hexdigest() != checksum:
            raise ValueError(f'Hash mismatch for {name}')
        if destination.is_file() and destination.stat().st_size == size:
            with destination.open('rb') as current:
                if hashlib.file_digest(current, 'sha256').hexdigest() == checksum:
                    return None
        scratch.replace(destination)
        return name
    finally:
        if scratch:
            scratch.unlink(missing_ok=True)


def notify_health(folder):
    seen_file = folder / '.monitor-notified.json'
    try:
        seen = json.loads(seen_file.read_text(encoding='utf-8')) if seen_file.is_file() else []
        report = json.loads((folder / 'v21_monitor.json').read_text(encoding='utf-8'))
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = Path(__file__).with_name('v21_notification.ps1')
        for event in report['events']:
            token = event['id']
            if token in seen:
                continue
            result = subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script),
                                     '-Title', event['title'], '-Text', event['message'], '-Tag', token[:16]],
                                    creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=15)
            if result.returncode:
                detail = result.stderr.decode(errors='replace').strip()
                logging.warning('Desktop notification pending for %s: %s', token, detail[:300])
                return
            logging.info('Desktop notification submitted: %s (%s)', event['kind'], token)
            seen.append(token)
            update = seen_file.with_suffix('.json.part')
            update.write_text(json.dumps(seen[-128:]), encoding='utf-8')
            update.replace(seen_file)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        logging.warning('Desktop notification will retry: %s', exc)


def receive(folder, log_file):
    import msvcrt
    import socket

    import paramiko

    from jetson_job import PYTHON, connect

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, encoding='utf-8', maxBytes=1_000_000, backupCount=2)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', handlers=[handler])
    logging.getLogger('paramiko').setLevel(logging.WARNING)
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    with (folder / '.receiver.lock').open('a+b') as guard:
        if guard.tell() == 0:
            guard.write(b'0')
            guard.flush()
        guard.seek(0)
        try:
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        while True:
            try:
                with connect() as client:
                    transport = client.get_transport()
                    transport.set_keepalive(15)
                    command = f'{PYTHON} -u {ROOT}/scripts/v21_sync.py send --root {ROOT}'
                    with transport.open_session(timeout=15) as channel:
                        channel.settimeout(75)
                        channel.exec_command(command)
                        logging.info('Connected to Jetson report stream')
                        with channel.makefile('rb') as stdout:
                            while True:
                                name = receive_file(stdout, folder)
                                if name == 'v21_monitor.json':
                                    notify_health(folder)
                                elif name and name != 'sync_status.json':
                                    logging.info('Synchronized %s', name)
            except (OSError, EOFError, socket.timeout, ValueError, TypeError, KeyError, paramiko.SSHException) as exc:
                logging.info('Reconnecting after %s: %s', type(exc).__name__, exc)
                time.sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('send', 'receive'))
    parser.add_argument('--root', type=Path, default=Path(ROOT))
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parents[1] / 'outputs/v21')
    parser.add_argument('--log', type=Path, default=Path(__file__).resolve().parents[1] / 'logs/tess-v21-sync.log')
    args = parser.parse_args()
    if args.action == 'send':
        try:
            send(args.root)
        except BrokenPipeError:
            pass
    else:
        receive(args.destination, args.log)


if __name__ == '__main__':
    main()
