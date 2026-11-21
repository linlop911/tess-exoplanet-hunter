"""Run the V2.1 rolling survey and its resource guard entirely on Jetson."""

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import requests

from golden_calibration import phase
from rolling_sources import (GIB, Ledger, NetworkBatch, TransferLimit, controls, eb_catalog, fetch_fits,
                             gaia_metadata, latest_sectors, metadata, science_targets, select_science_batch)
from tess_campaign import diagnose, environment, log, read_json
from tess_sources import atomic_json, digest, product, session
from tess_v21 import processing
from tess_vetting import catalogs, duration_days, match_catalog, pixel_check


def atomic_text(path, body):
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(body, encoding='utf-8')
    temporary.replace(path)


def stop_reason(root, reason):
    stamp = datetime.now(timezone.utc).isoformat()
    atomic_text(root / 'STOP_REASON.txt', f'{stamp}\n{reason}\n')
    log(f'Paused: {reason}')


def resource_issue(root, hot_since):
    if shutil.disk_usage('/').free < 25*GIB:
        return 'eMMC free space is below 25 GiB', hot_since
    if shutil.disk_usage(root).free < 100*GIB:
        return 'NVMe free space is below 100 GiB', hot_since
    rows = Path('/proc/meminfo').read_text().splitlines()
    swap = {line.split(':', 1)[0]: int(line.split()[1]) for line in rows if line.startswith(('SwapTotal:', 'SwapFree:'))}
    if swap['SwapTotal'] - swap['SwapFree'] > 1024*1024:
        return 'Swap usage exceeds 1024 MiB', hot_since
    readings = []
    for path in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            value = int(path.read_text()) / 1000
            if 0 < value < 160:
                readings.append(value)
        except (OSError, ValueError):
            continue
    if not readings:
        return 'Jetson temperature sensors are unavailable', hot_since
    now = time.monotonic()
    if max(readings) <= 80:
        return None, None
    hot_since = hot_since or now
    if now - hot_since >= 60:
        return f'Jetson temperature stayed above 80°C for 60 s (current {max(readings):.1f}°C)', hot_since
    return None, hot_since


def preflight(root):
    if os.geteuid() == 0:
        raise RuntimeError('Scanner requires an ordinary account')
    environment(root)
    issue, _ = resource_issue(root, None)
    if issue:
        raise RuntimeError(issue)


def supervise(args):
    root = args.root
    try:
        preflight(root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        stop_reason(root, str(exc))
        return 1
    if (root / 'STOP').exists():
        stop_reason(root, 'Manual STOP sentinel present')
        return 0
    (root / 'STOP_REASON.txt').unlink(missing_ok=True)
    worker = subprocess.Popen([sys.executable, __file__, *sys.argv[1:], '--worker'], start_new_session=True)
    hot_since = None
    try:
        while worker.poll() is None:
            cause, hot_since = resource_issue(root, hot_since)
            if cause:
                stop_reason(root, cause)
                os.killpg(worker.pid, signal.SIGTERM)
                try:
                    worker.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait()
                return 1
            time.sleep(5)
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait()
    return int(worker.returncode)


def calibration(root):
    marker = root / 'logs/v21_golden.exit'
    report = root / 'out/v21_golden/golden_report.json'
    if not report.is_file():
        log('Waiting for V2.1 golden regression on Jetson')
    for _ in range(240):
        if (root / 'STOP').exists():
            raise RuntimeError('Manual STOP sentinel present')
        if marker.exists():
            if marker.read_text().split()[-1] != '0' or not report.exists():
                raise RuntimeError('V2.1 golden regression failed')
            outcome = read_json(report)
            if not outcome['passed'] or outcome['search_code_sha256'] != digest(Path(__file__).with_name('tess_v21.py')):
                raise RuntimeError('Golden report failed or does not match the deployed V2.1 search')
            return digest(report)
        time.sleep(10)
    raise RuntimeError('Timed out waiting for V2.1 golden regression')


def freeze_batch(client, root, path, committed, pool='hybrid'):
    manifest = path / 'input_targets.json'
    truth_file = path / 'blind_key.json'
    if manifest.is_file():
        return read_json(manifest)
    cache = root / 'cache/rolling_v21'
    cache.mkdir(parents=True, exist_ok=True)
    # Share the immutable bulk indices with the previous Jetson campaign.
    old_cache = root / 'cache/selection'
    if old_cache.is_dir():
        for entry in old_cache.glob('sector-*.json'):
            link = cache / entry.name
            if not link.exists():
                link.hardlink_to(entry)
    selection = controls(client, cache)
    blocked = {str(row['tic']) for row in selection['truth']} | committed
    sectors = latest_sectors(client)
    log(f'Latest regular public MAST sector: {sectors[-1]} (target pool: {pool})')
    science = select_science_batch(client, cache, blocked, sectors, pool=pool, total=100, hidden_quota=50)
    if len(science) < 100:
        raise NetworkBatch(f'Only {len(science)} eligible unscanned targets remain in the available archive')
    control_ids = [str(row['tic']) for row in selection['targets']]
    control_meta = metadata(client, cache / 'tic_v21.json', control_ids)
    control_list = [{**item, 'stellar': control_meta.get(str(item['tic']), {})} for item in selection['targets']]
    gems_count = sum(1 for row in science if row.get('pool') == 'hidden_gems')
    science.sort(key=lambda row: (row.get('pool') != 'hidden_gems',
                                  row['stellar']['contamination'] is None, row['stellar']['contamination'] or 0,
                                  row['stellar']['tmag'], row['tic']))
    for position, item in zip((23, 77), control_list):
        science.insert(position, item)
    gaia_metadata(client, {row['tic']: row['stellar'] for row in science})
    atomic_json(truth_file, selection['truth'])
    atomic_json(manifest, science)
    log(f'{path.name}: frozen 100 science targets ({gems_count} hidden_gems, {100-gems_count} standard) and two blind controls')
    return science


def fetch_target(target, directory, ledger):
    with session() as client:
        return [fetch_fits(client, row, directory, ledger) for row in target['products']]


def crunch(targets, raw, ledger, out, workers):
    checkpoints = out / 'checkpoints'
    checkpoints.mkdir(exist_ok=True)
    done, failed, search_jobs = {}, [], {}
    with ThreadPoolExecutor(max_workers=2) as downloads, ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context('spawn')
    ) as pool:
        pending = {}
        for target in targets:
            tic = target['tic']
            checkpoint = checkpoints / f'{tic}.json'
            if checkpoint.exists():
                done[tic] = read_json(checkpoint)
            else:
                pending[downloads.submit(fetch_target, target, raw, ledger)] = target
        received = 0
        for future in as_completed(pending):
            target = pending[future]
            try:
                products = future.result()
                search_jobs[pool.submit(processing, raw, products, target['stellar'])] = target['tic']
                received += 1
                if received == 1 or received % 10 == 0:
                    log(f'{out.name}: {received}/{len(pending)} target downloads verified')
            except NetworkBatch as exc:
                failed.append(str(exc))
                log(f'{out.name}: download failed for TIC {target["tic"]}: {exc}')
        for future in as_completed(search_jobs):
            tic = search_jobs[future]
            row = future.result()
            atomic_json(checkpoints / f'{tic}.json', row)
            done[tic] = row
            if len(done) == 1 or len(done) % 10 == 0:
                log(f'{out.name}: {len(done)}/{len(targets)} searched and checkpointed')
    if failed:
        raise NetworkBatch(f'{len(failed)} incomplete light-curve targets: {failed[0]}')
    return [done[t['tic']] for t in targets]


def eb_match(signal, rows):
    p, epoch = signal['period_days'], signal['epoch_btjd']
    dur = duration_days(signal)
    for row in rows:
        rp, ref = float(row['period']), float(row['bjd0'])
        if ref > 2457000:
            ref -= 2457000
        if rp <= 0 or (p and not any(abs(p/(rp*f) - 1) <= .005 for f in (1, .5, 2, 1/3, 3))):
            continue
        if min(abs(phase(epoch, rp, ref)), abs(phase(epoch, rp, ref+rp/2))) <= dur/2:
            return {'tess_id': row['tess_id'], 'signal_id': row['signal_id'], 'period_days': rp, 'epoch_btjd': ref}
    return None


def screen(client, root, results):
    try:
        catalog = catalogs(client, (row['tic'] for row in results))
        eb = eb_catalog(client, root / 'cache/rolling_v21/tess-ebs.csv')
    except (requests.RequestException, ValueError, OSError) as exc:
        raise NetworkBatch(f'Catalog retrieval unsuccessful: {exc}') from exc
    hits = []
    for target in results:
        tic = target['tic']
        for item in target['candidates']:
            known = match_catalog(item, catalog[tic])
            binary = eb_match(item, eb[tic])
            if binary is not None:
                known.update(state='KNOWN_EB', eclipsing_binary=binary)
            vet = {**item['vet'], 'catalog': {'state': 'PASS' if known['state'] == 'UNCATALOGED' else 'FLAG'}}
            vet['pixel'] = {'state': 'INCONCLUSIVE', 'reason': 'Not selected for pixel check'}
            hits.append({**item, 'tic': tic, 'sectors': [p['sector'] for p in target['products']],
                         'catalog': known, 'vet': vet})
    return hits


def pixel(client, raw, led, signal, target):
    from astropy.io import fits
    import numpy as np

    points = []
    for row in target['products']:
        with fits.open(raw / row['filename'], memmap=True) as hdus:
            t = hdus[1].data['TIME']
            p = signal['period_days']
            offset = phase(t, p, signal['epoch_btjd']) if p else t-signal['epoch_btjd']
            points.append((int(np.count_nonzero((hdus[1].data['QUALITY'] == 0) &
                                                (abs(offset) < duration_days(signal)/2))), row))
    coverage, row = max(points, key=lambda pair: pair[0])
    if coverage < 20:
        return {'state': 'INCONCLUSIVE', 'reason': 'Insufficient pixel-phase coverage'}
    basename = row['filename'].removesuffix('_lc.fits')
    tpf = product(row['tic'], row['sector'], basename, 'tp')
    try:
        receipt = fetch_fits(client, tpf, raw, led)
        evidence = pixel_check(raw / receipt['filename'], signal)
        return {**evidence, 'product': receipt}
    except (NetworkBatch, TransferLimit, OSError, ValueError, KeyError) as exc:
        return {'state': 'INCONCLUSIVE', 'reason': str(exc)[:300]}


def rank(candidate):
    states = candidate['vet']
    return (candidate['catalog']['state'] == 'UNCATALOGED',
            -sum(item['state'] == 'FLAG' for item in states.values()),
            sum(item['state'] in ('PASS', 'NOT_APPLICABLE') for item in states.values()), candidate['snr'])


def high_priority(row):
    vet = row['vet']
    keys = ('odd_even', 'secondary', 'shape', 'sap_pdcsap', 'pixel', 'harmonics',
            'stellar_radius_ruwe', 'kepler_duration', 'catalog')
    return all(vet.get(key, {'state': 'NOT_APPLICABLE'})['state'] in ('PASS', 'NOT_APPLICABLE') for key in keys) and \
        vet['flare_window']['state'] in ('PASS', 'NOT_APPLICABLE')


def verify_controls(out, results):
    truth = read_json(out / 'blind_key.json')
    result = []
    for item in truth:
        target = results[item['tic']]
        match = next((row for row in target['candidates'] if row['period_days'] and
                      abs(row['period_days']/item['period_days']-1) <= .005 and
                      abs(phase(row['epoch_btjd'], item['period_days'], item['epoch_btjd'])) <= duration_days(row)/2), None)
        result.append({**item, 'recovered': match is not None, 'detected_period_days': match['period_days'] if match else None,
                       'period_error_pct': abs(match['period_days']/item['period_days']-1)*100 if match else None})
    return result


def save_csv(path, candidates):
    columns = ('batch', 'tic', 'sectors', 'kind', 'period_days', 'epoch_btjd', 'depth_ppm', 'duration_hours',
               'snr', 'mad_sde', 'rp_rearth', 'gaia_ruwe', 'pixel', 'catalog', 'high_priority', 'diagnostic')
    temporary = path.with_suffix('.csv.part')
    with temporary.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, columns)
        writer.writeheader()
        for row in candidates:
            vet = row['vet']
            writer.writerow({**{key: row.get(key) for key in columns}, 'sectors': ' '.join(map(str, row['sectors'])),
                             'duration_hours': duration_days(row)*24, 'rp_rearth': vet['stellar_radius_ruwe']['rp_rearth'],
                             'gaia_ruwe': vet['stellar_radius_ruwe']['gaia_ruwe'], 'pixel': vet['pixel']['state'],
                             'catalog': row['catalog']['state']})
    temporary.replace(path)


def publish(root, report, retained):
    out = root / 'out'
    master = out / 'MASTER_HITS.json'
    rows = read_json(master) if master.exists() else []
    last = report['batch']
    rows = [row for row in rows if row['batch'] != last] + retained
    atomic_json(master, rows)
    save_csv(out / 'MASTER_HITS.csv', rows)
    lines = ['# 待复核凌星候选信号', '']
    for row in rows:
        description = f"{row['period_days']:.6f} 天" if row['period_days'] else f"BTJD {row['epoch_btjd']:.6f}"
        lines.append(f"- {row['batch']} · TIC {row['tic']} · {description} · [四联图]({row['batch']}/{row['diagnostic']})")
    if not rows:
        lines.append('当前没有通过全部适用筛查的信号。')
    atomic_text(out / 'MASTER_HITS.md', '\n'.join(lines)+'\n')


def review_markdown(batch, rows, blind):
    lines = ['# 待复核凌星候选信号', '', f'批次 {batch}', '',
             f"盲测恢复 {sum(item['recovered'] for item in blind)} / {len(blind)}。"]
    for row in rows:
        star = row['vet']['stellar_radius_ruwe']
        pixel_state = row['vet']['pixel']
        period = f"周期 {row['period_days']:.8f} 天" if row['period_days'] else '单次事件'
        sde = f"，MAD-SDE {row['mad_sde']:.2f}" if row['mad_sde'] is not None else ''
        radius = f"{star['rp_rearth']:.2f} R⊕" if star['rp_rearth'] is not None else '待核验'
        ruwe = f"{star['gaia_ruwe']:.3f}" if star['gaia_ruwe'] is not None else '待核验'
        offset = pixel_state.get('centroid_offset_px')
        centroid = f'，偏移 {offset:.2f} 像素' if offset is not None else ''
        lines.extend(['', f'## TIC {row["tic"]}', '',
                      f"- 扇区 {', '.join(map(str, row['sectors']))}。{period}，BTJD {row['epoch_btjd']:.6f}。",
                      f"- 深度 {row['depth_ppm']:.0f} ppm，持续时间 {duration_days(row)*24:.2f} 小时。",
                      f"- SNR {row['snr']:.2f}{sde}，推算半径 {radius}，Gaia DR3 RUWE {ruwe}。",
                      f"- 像素检查 {pixel_state['state']}{centroid}。[四联图]({batch}/{row['diagnostic']})"])
    lines.extend(['', '## 独立复核', '',
                  '- 对照盲测结果、原始光变、掩蔽窗口和观测缺口。用 BTJD 对齐全部历元并检查 2P。',
                  '- 用相同 TIC 和明确列出的扇区运行 SHERLOCK 独立周期搜索，比较周期、历元、深度和谐波。ExoMiner++ 作为另一路复检，先核对模型所需的 TESS DV 产品是否可得。',
                  '- 在 ExoFOP 查 TOI、社区候选、目标注释和附近食双星，按扇区对照差分图。[ExoFOP](https://exofop.ipac.caltech.edu/tess/)'])
    return '\n'.join(lines)+'\n'


def cleanup(root, raw, targets, keep):
    if raw.resolve().parent != (root / 'raw').resolve() or not raw.name.startswith('v21_batch_'):
        raise RuntimeError(f'Refusing cleanup outside the active batch: {raw}')
    removable_tics = {row['tic'] for row in targets if row['tic'] not in keep}
    permitted = {item['filename'] for row in targets for item in row['products'] if row['tic'] in removable_tics}
    allowed = re.compile(r'tess\d+-s\d{4}-\d{16}-\d{4}-s_lc\.fits')
    removed = 0
    for filename in sorted(permitted):
        if not allowed.fullmatch(filename):
            raise RuntimeError(f'Refusing unexpected cleanup filename: {filename}')
        for tag in (filename, filename.removesuffix('.fits') + '.receipt.json'):
            path = raw / tag
            if path.is_file():
                path.unlink()
                removed += 1
    generated = re.compile(r'tess\d+-s\d{4}-(\d{16})-\d{4}-s_(?:lc|tp)'
                           r'(?:\.(?:aws|mast)\.part(?:\.json)?|\.receipt\.json|\.fits)')
    for path in raw.iterdir():
        match = generated.fullmatch(path.name)
        if path.is_file() and match and int(match[1]) in removable_tics:
            path.unlink()
            removed += 1
    return removed


def execute_batch(args, client, state, calibration_sha):
    root = args.root
    tag = f"v21_batch_{state['batch']:05d}"
    out, raw = root / 'out' / tag, root / 'raw' / tag
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    report_file = out / 'batch_status.json'
    targets = freeze_batch(client, root, out, set(map(str, state['scanned_tic_ids'])),
                           pool=getattr(args, 'target_pool', 'hybrid'))
    if report_file.is_file():
        report = read_json(report_file)
        return report, targets
    ledger = Ledger(out / 'transfer_ledger.json')
    started = time.monotonic()
    results = crunch(targets, raw, ledger, out, args.workers)
    by_target = {row['tic']: row for row in results}
    values = sorted(screen(client, root, results), key=rank, reverse=True)
    eligible_stage = [row for row in values if row['catalog']['state'] == 'UNCATALOGED' and
                      all(check['state'] != 'FLAG' for check in row['vet'].values())]
    singles_stage = [row for row in eligible_stage if row.get('kind') == 'single'][:3]
    staged = singles_stage + [row for row in eligible_stage if row not in singles_stage][:15 - len(singles_stage)]
    for index, row in enumerate(staged, 1):
        row['vet']['pixel'] = pixel(client, raw, ledger, row, by_target[row['tic']])
        row['high_priority'] = high_priority(row)
        row['diagnostic'] = diagnose(out, raw, by_target[row['tic']], row, f'candidate_{index:02d}')
    for row in values:
        row['batch'] = tag
        row.setdefault('high_priority', False)
    values.sort(key=rank, reverse=True)
    atomic_json(out / 'candidates_ranked.json', values)
    save_csv(out / 'candidates_ranked.csv', values)
    blindness_sha = digest(out / 'candidates_ranked.json')
    reviewed = verify_controls(out, by_target)
    blind_pass = all(row['recovered'] for row in reviewed)
    high = [row for row in values if row['high_priority']] if blind_pass else []
    report = {'batch': tag, 'searched': len(results), 'science': 100, 'candidates': len(values),
              'pixel_reviews': len(staged), 'high_priority': len(high), 'blind': reviewed, 'blind_pass': blind_pass,
              'science_ids': [row['tic'] for row in targets if row['tic'] not in {control['tic'] for control in reviewed}],
              'ranking_before_unblind_sha256': blindness_sha, 'golden_report_sha256': calibration_sha, 'transfer_bytes': ledger.used,
              'elapsed_s': round(time.monotonic()-started, 2), 'disk_nvme_free_gib': shutil.disk_usage(root).free/GIB}
    sketch = [f'# {tag}', '', f"- 已处理 {report['searched']} 颗，包括 100 颗科学目标和 2 颗盲测。",
              f"- 初筛候选 {len(values)} 条，像素核验 {len(staged)} 条，高优待复核信号 {len(high)} 条。",
              f"- 下载计账 {ledger.used/GIB:.3f} GiB，NVMe 余量 {report['disk_nvme_free_gib']:.1f} GiB。",
              f"- 盲测 {'通过' if blind_pass else '失败'}。"]
    atomic_text(out / 'batch_summary.md', '\n'.join(sketch)+'\n')
    atomic_json(out / 'high_priority.json', high)
    atomic_json(report_file, report)
    return report, targets


def commit(args, report, targets, state, state_path):
    tag = report['batch']
    out = args.root / 'out' / tag
    high = read_json(out / 'high_priority.json')
    keep = {row['tic'] for row in high} | {row['tic'] for row in report['blind']}
    publish(args.root, report, high)
    if not report['blind_pass']:
        atomic_json(state_path, state)
        return 'Blind recovery failed or period deviation exceeded 0.5%'
    removed = cleanup(args.root, args.root / 'raw' / tag, targets, keep)
    atomic_json(out / 'retention.json', {'kept_tics': sorted(keep), 'removed_files': removed})
    state['scanned_tic_ids'] = sorted(set(state['scanned_tic_ids']) | set(report['science_ids']))
    state.update(batch=state['batch']+1, failures=0, last_batch=tag)
    atomic_json(state_path, state)
    log(f"{tag}: committed {report['science']} science targets, {removed} unneeded files removed")
    if high:
        atomic_text(args.root / 'out' / f'HIT_ALERT_{tag}.md', review_markdown(tag, high, report['blind']))
        if args.mode == 'stop_on_hit':
            return f'{tag}: {len(high)} high-priority uncataloged candidate signals'
    return None


def run(args):
    root = args.root
    directory = root / 'state'
    directory.mkdir(exist_ok=True)
    state_path = directory / 'scanner_state.json'
    state = read_json(state_path) if state_path.is_file() else {'batch': 1, 'scanned_tic_ids': [], 'failures': 0}
    atomic_json(state_path, state)
    try:
        calibration_sha = calibration(root)
        log('V2.1 golden gate passed; rolling pilot is active')
        attempts = 0
        last_batch = getattr(args, 'last_batch', None)
        with session() as client:
            while ((not args.max_batches or attempts < args.max_batches) and
                   (last_batch is None or state['batch'] <= last_batch)):
                if (root / 'STOP').exists():
                    stop_reason(root, 'Manual STOP sentinel reached between batches')
                    return 0
                preflight(root)
                if state['failures'] >= 3:
                    raise RuntimeError('Three consecutive batches failed data acquisition or validation')
                attempts += 1
                try:
                    report, targets = execute_batch(args, client, state, calibration_sha)
                except (NetworkBatch, requests.RequestException) as exc:
                    state['failures'] += 1
                    state['last_error'] = str(exc)
                    atomic_json(state_path, state)
                    log(f"Data acquisition failure {state['failures']}/3: {exc}")
                    if state['failures'] >= 3:
                        raise RuntimeError('Three consecutive batches failed data acquisition or validation') from exc
                    for _ in range(6):
                        if (root / 'STOP').exists():
                            stop_reason(root, 'Manual STOP sentinel reached after an interrupted batch')
                            return 0
                        time.sleep(5)
                    continue
                cause = commit(args, report, targets, state, state_path)
                if cause:
                    stop_reason(root, cause)
                    return 0 if report['blind_pass'] else 2
        if last_batch is not None and state['batch'] > last_batch:
            stop_reason(root, f'Increment limit reached (through batch {last_batch})')
        else:
            stop_reason(root, f'Pilot limit reached ({args.max_batches} batch attempts)')
        return 0
    except Exception as exc:
        stop_reason(root, f'{type(exc).__name__}: {exc}')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/data/astro_tess'))
    parser.add_argument('--mode', choices=('stop_on_hit', 'continuous'), required=True)
    parser.add_argument('--max-batches', type=int, default=0)
    parser.add_argument('--last-batch', type=int, help='inclusive final committed batch number')
    parser.add_argument('--target-pool', choices=('hybrid', 'hidden_gems', 'standard'), default='hybrid')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.max_batches < 0 or args.workers < 1 or (args.last_batch is not None and args.last_batch < 1):
        parser.error('Expected a nonnegative batch limit and at least one worker')
    args.root = args.root.resolve()
    return run(args) if args.worker else supervise(args)


if __name__ == '__main__':
    raise SystemExit(main())
