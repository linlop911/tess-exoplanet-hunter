"""Deploy and inspect this project's own files over a verified Jetson SSH session."""

import argparse
import csv
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

import paramiko


LOCAL = Path(__file__).resolve().parent
PROJECT = LOCAL.parent
WORKSPACE = PROJECT.parent
ROOT = os.environ.get('TESS_JETSON_ROOT', '/home/data/astro_tess')
PYTHON = os.environ.get('TESS_JETSON_PYTHON', '/home/data/miniforge3/envs/astro/bin/python')
HOST = os.environ.get('TESS_JETSON_HOST', '192.168.55.1')


def _load_ssh_credentials():
    user = os.environ.get('TESS_JETSON_USER')
    password = os.environ.get('TESS_JETSON_PASS')
    if user and password:
        return HOST, user, password
    info_path = next(PROJECT.glob('Jetson-SSH*.txt'), None) or next(WORKSPACE.glob('Jetson-SSH*.txt'), None)
    if not info_path:
        raise RuntimeError('Set TESS_JETSON_USER and TESS_JETSON_PASS (see .env.example)')
    text = info_path.read_text(encoding='utf-8', errors='replace')
    kv = [m.group(1).strip() for m in re.finditer(r'[:：]\s*([A-Za-z0-9_@.-]+)', text)]
    creds = [v for v in kv if not re.fullmatch(r'\d+(?:\.\d+){3}|[0-9a-fA-F:]+', v)]
    if len(creds) < 2:
        raise RuntimeError('Could not parse SSH credentials; set TESS_JETSON_USER and TESS_JETSON_PASS')
    return HOST, creds[0], creds[1]


def connect():
    host, username, password = _load_ssh_credentials()
    client = paramiko.SSHClient()
    client.load_host_keys(str(Path(os.environ['USERPROFILE']) / '.ssh' / 'known_hosts'))
    client.connect(host, username=username, password=password, timeout=10, banner_timeout=10, auth_timeout=10,
                   allow_agent=False, look_for_keys=False)
    return client


def run(client, command):
    _, stdout, stderr = client.exec_command(command, timeout=18)
    output = stdout.read().decode('utf-8', 'replace')
    errors = stderr.read().decode('utf-8', 'replace')
    code = stdout.channel.recv_exit_status()
    if code:
        raise RuntimeError(f'Jetson exit {code}\n{output}{errors}')
    return output + errors


def upload(client, names):
    with client.open_sftp() as sftp:
        for name in names:
            target = f'{ROOT}/scripts/{name}'
            sftp.put(str(LOCAL / name), target + '.part')
            sftp.posix_rename(target + '.part', target)


def launch(client, name, files, invocation):
    log = f'{ROOT}/logs/{name}.log'
    lock = f'{ROOT}/logs/{name}.lock'
    run(client, f'flock -n {lock} true')
    upload(client, files)
    body = (f'export MPLCONFIGDIR={ROOT}/cache/matplotlib PYTHONDONTWRITEBYTECODE=1 '
            'OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1; '
            f'stamp=$(date -u +%Y%m%dT%H%M%S); '
            f'{{ {invocation}; }} >>{log} 2>&1; rc=$?; '
            f'printf "%s %s\\n" "$stamp" "$rc" > {ROOT}/logs/{name}.exit; exit "$rc"')
    command = f'nohup flock -n {lock} bash -c {shlex.quote(body)} </dev/null >/dev/null 2>&1 & echo $!'
    return run(client, command)


def ensure_monitor(client):
    lock = f'{ROOT}/logs/v21_monitor.lock'
    if run(client, f'if flock -n {lock} true; then echo idle; else echo active; fi').strip() == 'idle':
        launch(client, 'v21_monitor', ('v21_monitor.py', 'v21_sync.py'),
               f'{PYTHON} {ROOT}/scripts/v21_monitor.py --root {ROOT}')


def retrieve(client):
    destination = PROJECT / 'outputs'
    destination.mkdir(exist_ok=True)
    with client.open_sftp() as sftp:
        for filename in ('campaign.log', 'campaign.exit'):
            direct = f'{ROOT}/logs/{filename}'
            try:
                copy_file(sftp, direct, destination / filename)
            except FileNotFoundError:
                pass
        for name in sftp.listdir(f'{ROOT}/out'):
            if name == 'campaign_progress.json':
                copy_file(sftp, f'{ROOT}/out/{name}', destination / name)
                continue
            if not (name.startswith('batch_') or name == 'golden_expanded'):
                continue
            directory = f'{ROOT}/out/{name}'
            output = destination / name
            verified = 'golden_report.json' if name == 'golden_expanded' else 'batch_status.json'
            items = sftp.listdir(directory)
            if verified not in items:
                continue
            output.mkdir(parents=True, exist_ok=True)
            for item in items:
                if item.endswith(('.csv', '.json', '.md', '.png')) and not item.startswith('.'):
                    copy_file(sftp, f'{directory}/{item}', output / item)


def copy_file(sftp, remote, local):
    stat = sftp.stat(remote)
    if local.is_file() and local.stat().st_size == stat.st_size and int(local.stat().st_mtime) == stat.st_mtime:
        return
    scratch = local.with_suffix(local.suffix + '.part')
    sftp.get(remote, str(scratch))
    os.utime(scratch, (stat.st_atime, stat.st_mtime))
    scratch.replace(local)


def retrieve_v21(client):
    destination = PROJECT / 'outputs' / 'v21'
    destination.mkdir(parents=True, exist_ok=True)
    with client.open_sftp() as sftp:
        for name in ('STOP_REASON.txt', 'state/scanner_state.json', 'logs/rolling_v21.log', 'logs/v21_smoke.log'):
            try:
                copy_file(sftp, f'{ROOT}/{name}', destination / name.rsplit('/', 1)[-1])
            except FileNotFoundError:
                pass
        remote_out = f'{ROOT}/out'
        for name in sftp.listdir(remote_out):
            if name.startswith(('MASTER_HITS.', 'HIT_ALERT_v21_')):
                copy_file(sftp, f'{remote_out}/{name}', destination / name)
            elif name == 'v21_golden' or name.startswith('v21_batch_'):
                files = sftp.listdir(f'{remote_out}/{name}')
                if not {'golden_report.json', 'batch_status.json'}.intersection(files):
                    continue
                local = destination / name
                local.mkdir(exist_ok=True)
                for item in files:
                    if item.endswith(('.csv', '.json', '.md', '.png')):
                        copy_file(sftp, f'{remote_out}/{name}/{item}', local / item)


def watch():
    exit_path = f'{ROOT}/logs/campaign.exit'
    print('Monitoring Jetson campaign and syncing completed results', flush=True)
    for _ in range(1440):
        try:
            with connect() as client:
                current = run(client, f'if flock -n {ROOT}/logs/campaign.lock true; then '
                                      f'if test -s {exit_path}; then cat {exit_path}; else echo pending; fi; '
                                      'else echo running; fi').strip()
                retrieve(client)
                if current not in ('running', 'pending'):
                    print(f'Jetson campaign exit: {current}', flush=True)
                    return int(current.split()[-1])
        except (OSError, paramiko.SSHException, RuntimeError) as exc:
            print(f'Sync retry: {type(exc).__name__}', flush=True)
        time.sleep(60)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('golden', 'select', 'start', 'status', 'retrieve', 'watch', 'command', 'diagnostic',
                                           'v21-golden', 'v21-start', 'v21-status', 'v21-smoke', 'v21-export', 'v21-sync-deploy',
                                           'v21-increment', 'v21-monitor', 'v21-deep-catalog', 'v21-deep-vet', 'v21-deep-check',
                                           'v21-deep-export'))
    parser.add_argument('remote_command', nargs='?')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    if args.action == 'watch':
        raise SystemExit(watch())
    with connect() as client:
        if args.action == 'v21-golden':
            print(launch(client, 'v21_golden', ('tess_vetting.py', 'tess_v21.py', 'rolling_sources.py', 'rolling_scanner.py'),
                         f'{PYTHON} {ROOT}/scripts/tess_v21.py {ROOT}'), end='')
        elif args.action == 'v21-start':
            with client.open_sftp() as sftp:
                try:
                    with sftp.open(f'{ROOT}/state/scanner_state.json', 'rb') as state_file:
                        next_batch = json.load(state_file)['batch']
                except FileNotFoundError:
                    next_batch = 1
            remaining = 6 - next_batch
            if not 1 <= remaining <= 5:
                parser.error('The five-batch pilot has already reached its limit')
            ensure_monitor(client)
            invocation = (f'{PYTHON} {ROOT}/scripts/rolling_scanner.py --root {ROOT} '
                          f'--mode stop_on_hit --max-batches {remaining} --workers 4')
            print(launch(client, 'rolling_v21', ('rolling_sources.py', 'rolling_scanner.py'), invocation), end='')
        elif args.action == 'v21-smoke':
            print(launch(client, 'v21_smoke', ('rolling_smoke.py',),
                         f'{PYTHON} {ROOT}/scripts/rolling_smoke.py {ROOT}'), end='')
        elif args.action == 'v21-increment':
            try:
                first = int(args.remote_command or '')
            except ValueError:
                parser.error('v21-increment requires the first batch number')
            if first < 1:
                parser.error('The first batch number must be positive')
            ensure_monitor(client)
            last = first+4
            invocation = (f'{PYTHON} {ROOT}/scripts/v21_increment.py --root {ROOT} '
                          f'--first-batch {first} --last-batch {last}')
            print(launch(client, 'increment_v21', ('rolling_scanner.py', 'v21_increment.py'), invocation), end='')
        elif args.action == 'v21-export':
            retrieve_v21(client)
        elif args.action == 'v21-sync-deploy':
            upload(client, ('v21_sync.py',))
            print(run(client, f'{PYTHON} {ROOT}/scripts/v21_sync.py --help'), end='')
        elif args.action == 'v21-monitor':
            ensure_monitor(client)
            print(run(client, f'if ! flock -n {ROOT}/logs/v21_monitor.lock true; then echo MONITOR_ACTIVE; else exit 1; fi'), end='')
        elif args.action == 'v21-deep-catalog':
            print(launch(client, 'v21_deep_catalog', ('deep_vet_catalog.py',),
                         f'{PYTHON} {ROOT}/scripts/deep_vet_catalog.py {ROOT}'), end='')
        elif args.action == 'v21-deep-vet':
            files = ('deep_vet.py', 'deep_vet_catalog.py', 'deep_vet_photometry.py', 'deep_vet_pixels.py')
            print(launch(client, 'v21_deep_vet', files, f'{PYTHON} {ROOT}/scripts/deep_vet.py {ROOT}'), end='')
        elif args.action == 'v21-deep-check':
            upload(client, ('deep_vet_photometry.py',))
            print(run(client, f'PYTHONDONTWRITEBYTECODE=1 {PYTHON} {ROOT}/scripts/deep_vet_photometry.py {ROOT}/golden_expanded'), end='')
        elif args.action == 'v21-deep-export':
            source = f'{ROOT}/out/deep_vet_b2_b6'
            destination = PROJECT / 'outputs/v21/deep_vet_top10'
            destination.mkdir(parents=True, exist_ok=True)
            with client.open_sftp() as sftp:
                sftp.stat(f'{source}/DEEP_VET_REPORT.md')
                for name in sftp.listdir(source):
                    if (name in {'MASTER_HITS_deep_vetted.csv', 'DEEP_VET_REPORT.md', 'evidence.json', 'catalog_audit.json',
                                 'transfer_ledger.json', 'review_audit.json'} or
                            re.fullmatch(r'TOP\d+_TIC\d+\.png', name)):
                        copy_file(sftp, f'{source}/{name}', destination / name)
            csv_path = destination / 'MASTER_HITS_deep_vetted.csv'
            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                expected = {row['figure'] for row in csv.DictReader(stream) if row['final_grade'] == 'KEEP_TOP'}
            for path in destination.glob('TOP*_TIC*.png'):
                if re.fullmatch(r'TOP\d+_TIC\d+\.png', path.name) and path.name not in expected:
                    archive = destination.with_name('deep_vet_qa_preliminary')
                    archive.mkdir(exist_ok=True)
                    path.replace(archive / path.name)
        elif args.action == 'v21-status':
            print(run(client, f'for job in v21_golden rolling_v21 increment_v21 v21_monitor; do echo "[$job]"; '
                              f'if test -f {ROOT}/logs/$job.log; then tail -n 15 {ROOT}/logs/$job.log; fi; '
                              f'if ! flock -n {ROOT}/logs/$job.lock true; then echo running; '
                              f'elif test -f {ROOT}/logs/$job.exit; then cat {ROOT}/logs/$job.exit; fi; done; '
                              f'if test -f {ROOT}/STOP_REASON.txt; then cat {ROOT}/STOP_REASON.txt; fi'), end='')
        elif args.action == 'golden':
            invocation = f'{PYTHON} {ROOT}/scripts/tess_sources.py golden && '
            invocation += f'if test -e {ROOT}/out/golden_expanded/golden_report.json; then '
            invocation += f'mv {ROOT}/out/golden_expanded {ROOT}/out/golden_$(date -u +%Y%m%dT%H%M%S); fi && '
            invocation += f'{PYTHON} {ROOT}/scripts/golden_calibration.py {ROOT}/golden_expanded {ROOT}/out/golden_expanded'
            print(launch(client, 'golden_expanded', ('tess_sources.py', 'golden_calibration.py'), invocation), end='')
        elif args.action in ('select', 'start'):
            invocation = f'{PYTHON} {ROOT}/scripts/tess_campaign.py --root {ROOT} --total 1000 --batch-size 100 --workers 4'
            if args.action == 'select':
                invocation += ' --select-only'
            print(launch(client, 'campaign', ('tess_sources.py', 'tess_vetting.py', 'tess_campaign.py'), invocation), end='')
        elif args.action == 'diagnostic':
            upload(client, ('diagnose_sectors.py',))
            option = ' --smoke' if args.remote_command == 'smoke' else ''
            print(run(client, f'{PYTHON} {ROOT}/scripts/diagnose_sectors.py {ROOT}/golden_expanded{option}'), end='')
        elif args.action == 'status':
            print(run(client, f'for job in golden_expanded campaign; do '
                              f'echo "[$job]"; '
                              f'if test -f {ROOT}/logs/$job.log; then tail -n 15 {ROOT}/logs/$job.log; fi; '
                              f'if ! flock -n {ROOT}/logs/$job.lock true; then echo running; '
                              f'elif test -f {ROOT}/logs/$job.exit; then cat {ROOT}/logs/$job.exit; fi; done'), end='')
        elif args.action == 'retrieve':
            retrieve(client)
        elif args.remote_command:
            print(run(client, args.remote_command), end='')
        else:
            parser.error('command requires a shell command')


if __name__ == '__main__':
    main()
