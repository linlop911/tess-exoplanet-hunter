"""Run a resumable Jetson campaign in independent, budget-limited batches."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import fcntl
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import numpy as np
import requests
from astropy.io.fits.verify import VerifyError

from golden_calibration import MIN_SDE, MIN_SNR, detrend, periodic_search, phase, read_curve, single_search
from tess_sources import BudgetExceeded, TAP, atomic_json, digest, download, mast, product, session
from tess_vetting import catalogs, duration_days, lightcurve_checks, match_catalog, pixel_check, robust_sigma


BATCH_LIMIT = 3 * 1024**3
FORBIDDEN_SERVICES = ('zerotier-one.service', 'report_robot_network_info.service')
SECTOR_PAIRS = ((4, 5), (6, 7), (8, 9), (10, 11), (2, 3), (27, 28), (29, 30))


def log(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def environment(root):
    if shutil.disk_usage('/').free < 25 * 1024**3:
        raise RuntimeError('Root partition fell below 25 GiB')
    if root.stat().st_dev == Path('/').stat().st_dev or shutil.disk_usage(root).free < BATCH_LIMIT:
        raise RuntimeError('NVMe mount or working space is unavailable')
    for name in FORBIDDEN_SERVICES:
        state = subprocess.check_output(['systemctl', 'show', name, '--property=ActiveState,UnitFileState'], text=True)
        if set(state.splitlines()) != {'ActiveState=inactive', 'UnitFileState=disabled'}:
            raise RuntimeError(f'Unexpected service state: {name}: {state.strip()}')
    if subprocess.check_output(['ss', '-H', '-ltn', 'sport = :8080'], text=True).strip():
        raise RuntimeError('Port 8080 has a listener')


def wait_for_calibration(root):
    report = root / 'out/golden_expanded/golden_report.json'
    marker = root / 'logs/golden_expanded.exit'
    if not report.exists():
        log('Awaiting expanded golden regression')
    for _ in range(180):
        lock_path = root / 'logs/golden_expanded.lock'
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(10)
                continue
        if report.exists() and marker.exists():
            result = read_json(report)
            if (result.get('passed') and marker.read_text().split()[-1] == '0' and
                    result.get('search_code_sha256') == digest(Path(__file__).with_name('golden_calibration.py'))):
                return digest(report)
            raise RuntimeError('Expanded golden regression did not meet the production thresholds')
        if marker.exists() and marker.read_text().split()[-1] != '0':
            raise RuntimeError('Expanded golden regression exited without a passing report')
        time.sleep(10)
    raise RuntimeError('Expanded golden regression timed out')


def sector_products(client, cache, sector):
    path = cache / f'sector-{sector:04d}.json'
    if path.is_file():
        return read_json(path)
    listing = f'https://archive.stsci.edu/missions/tess/download_scripts/sector/tesscurl_sector_{sector}_lc.sh'
    response = client.get(listing, timeout=(8, 45))
    response.raise_for_status()
    pattern = re.compile(rf'(tess\d+-s{sector:04d}-(\d{{16}})-\d{{4}}-s)_lc\.fits')
    data = {}
    for obs, raw_tic in sorted(set(pattern.findall(response.text))):
        tic = int(raw_tic)
        data[str(tic)] = product(tic, sector, obs)
    if not data:
        raise RuntimeError(f'No 120-second SPOC products returned for sector {sector}')
    atomic_json(path, data)
    log(f'Sector {sector}: {len(data)} unique 120-second SPOC targets')
    return data


def stellar_rows(cache):
    path = cache / 'bright-dwarfs.json'
    if path.exists():
        return read_json(path)
    return {}


def eligible_stars(client, cache, ids, stars):
    unknown = sorted(set(ids) - set(stars), key=int)
    for index in range(0, len(unknown), 300):
        group = unknown[index:index + 300]
        filters = [{'paramName': 'ID', 'values': group}, {'paramName': 'Tmag', 'values': [{'min': -5, 'max': 11.5}]}]
        result = mast(client, 'Mast.Catalogs.Filtered.Tic', '*', filters, page_size=len(group) + 5)
        if result['paging']['pagesFiltered'] > 1:
            raise RuntimeError('TIC query unexpectedly paginated')
        stars.update(dict.fromkeys(group))
        for row in result['data']:
            tmag, teff, logg = (row.get(field) for field in ('Tmag', 'Teff', 'logg'))
            if None in (tmag, teff, logg) or not (tmag < 11.5 and 2500 <= teff <= 6200 and logg >= 4.0):
                continue
            if row.get('lumclass') not in ('DWARF', None):
                continue
            stars[str(row['ID'])] = {'tmag': tmag, 'teff': teff, 'logg': logg,
                                     'contamination': row.get('contratio')}
        atomic_json(cache / 'bright-dwarfs.json', stars)
        if index == 0 or (index // 300 + 1) % 5 == 0:
            log(f'TIC metadata: {min(index + len(group), len(unknown))}/{len(unknown)} checked for this sector pair')
    return {tic: stars[tic] for tic in ids if stars.get(tic)}


def control_candidates(client, cache):
    path = cache / 'control_candidates.csv'
    if not path.exists():
        query = ('select tic_id,pl_name,pl_orbper,pl_tranmid,pl_trandep from ps where default_flag=1 '
                 'and pl_orbper between 0.8 and 10 and pl_trandep>=0.10 and pl_tranmid is not null and tic_id is not null')
        response = client.get(TAP, params={'query': query, 'format': 'csv'}, timeout=(8, 60))
        response.raise_for_status()
        if not response.text.startswith('tic_id,pl_name,'):
            raise ValueError('Unexpected PS response while choosing controls')
        path.write_text(response.text, encoding='utf-8')
    rows = csv.DictReader(io.StringIO(path.read_text(encoding='utf-8')))
    return sorted(rows, key=lambda row: (-float(row['pl_trandep']), row['tic_id'], row['pl_name']))


def build_manifest(root, client, total):
    target_path, truth_path = root / 'targets.json', root / 'blind_key.json'
    if target_path.exists() and truth_path.exists():
        rows = read_json(target_path)
        if len(rows) != total or len({row['tic'] for row in rows}) != total:
            raise RuntimeError('The frozen target manifest does not match this campaign')
        return rows
    cache = root / 'cache/selection'
    cache.mkdir(parents=True, exist_ok=True)
    stars, eligible, candidates = stellar_rows(cache), {}, {}
    controls = control_candidates(client, cache)
    truth = []
    for pair in SECTOR_PAIRS:
        a, b = [sector_products(client, cache, sector) for sector in pair]
        overlap = (set(a) & set(b)) - {'261136679', '150428135'}
        for tic in overlap:
            candidates.setdefault(tic, [a[tic], b[tic]])
        eligible.update(eligible_stars(client, cache, overlap, stars))
        truth = []
        chosen = set()
        for row in controls:
            tid = row['tic_id'].removeprefix('TIC ')
            if tid in candidates and tid not in chosen:
                truth.append({'tic': int(tid), 'planet': row['pl_name'], 'period_days': float(row['pl_orbper']),
                              'epoch_btjd': float(row['pl_tranmid']) - 2457000})
                chosen.add(tid)
            if len(truth) == 2:
                break
        log(f'Sectors {pair}: {len(eligible)} eligible dwarfs, {len(truth)} controls available')
        if len(eligible) >= total and len(truth) == 2:
            break
    if len(eligible) < total or len(truth) != 2:
        raise RuntimeError(f'Insufficient eligible sources ({len(eligible)}) or controls ({len(truth)})')
    scientific = sorted((tic for tic in eligible if tic not in chosen),
                        key=lambda tic: (eligible[tic]['contamination'] is None,
                                         eligible[tic]['contamination'] or 0, eligible[tic]['tmag'], int(tic)))
    ids = scientific[:total - 2]
    for position, item in zip((23, 72), truth):
        ids.insert(position, str(item['tic']))
    rows = [{'tic': int(tic), 'products': candidates[tic], 'stellar': eligible.get(tic)} for tic in ids]
    atomic_json(truth_path, truth)
    atomic_json(target_path, rows)
    return rows


class DownloadBudget:
    def __init__(self, path):
        self.path = path
        self.charged = read_json(path)['charged_bytes'] if path.exists() else 0

    def reserve(self, size):
        if self.charged + size > BATCH_LIMIT:
            raise BudgetExceeded(f'Batch byte budget: {self.charged:,} + {size:,} exceeds {BATCH_LIMIT:,}')
        self.charged += size
        atomic_json(self.path, {'charged_bytes': self.charged, 'cap_bytes': BATCH_LIMIT})


def series(directory, manifest):
    curves = [read_curve(directory, item) for item in manifest]
    t, y, e = [np.concatenate(arrays) for arrays in zip(*(detrend(*curve) for curve in curves))]
    if not np.all(np.diff(t) > 0):
        raise ValueError('Stitched time is not increasing')
    return (t, y, e), curves


def covered_peak(event, periodic):
    return any(abs(phase(event['epoch_btjd'], peak['period_days'], peak['epoch_btjd'])) <=
               max(duration_days(peak), event['duration_hours'] / 24) / 2 for peak in periodic)


def process_target(directory, manifest):
    (t, y, e), _ = series(directory, manifest)
    peaks = periodic_search(t, y, e)
    periodic = [{**peak, 'kind': 'periodic'} for peak in peaks if peak['snr'] >= MIN_SNR and peak['sde'] >= MIN_SDE]
    raw_single = single_search(t, y)
    single = [{**event, 'kind': 'single', 'period_days': None, 'sde': None}
              for event in raw_single if not covered_peak(event, periodic)]
    candidates = []
    for candidate in periodic + single[:5]:
        checks = lightcurve_checks(t, y, candidate)
        candidates.append({**candidate, 'vet': checks})
    bins = np.floor((t - t[0]) * 24).astype(int)
    counts = np.bincount(bins)
    fluxes = np.bincount(bins, weights=y)
    good = counts >= 20
    scatter = float(robust_sigma(fluxes[good] / counts[good]) * 1e6) if np.any(good) else None
    return {'tic': manifest[0]['tic'], 'products': manifest, 'clean_points': len(t), 'hour_mad_ppm': scatter,
            'raw_single_count': len(raw_single), 'unmatched_single_count': len(single),
            'periodic_peaks': peaks, 'candidates': candidates}


def ranking(item):
    flags = sum(result['state'] == 'FLAG' for result in item['vet'].values())
    novelty = item['catalog']['state'] == 'UNCATALOGED'
    weight = item['sde'] / MIN_SDE if item['kind'] == 'periodic' else item['snr'] / 5.5
    return (flags == 0, novelty, weight, item['snr'], -item['tic'])


def candidate_outputs(out, records):
    atomic_json(out / 'candidates_ranked.json', records)
    scratch = out / 'candidates_ranked.csv.part'
    fields = ('tic', 'kind', 'period_days', 'epoch_btjd', 'depth_ppm', 'duration_hours', 'snr', 'sde',
              'catalog', 'odd_even', 'secondary', 'shape', 'flare_window', 'pixel', 'diagnostic')
    with scratch.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for candidate in records:
            writer.writerow({**{key: candidate.get(key) for key in fields},
                             'duration_hours': 24 * duration_days(candidate), 'catalog': candidate['catalog']['state'],
                             **{name: row['state'] for name, row in candidate['vet'].items()}})
    scratch.replace(out / 'candidates_ranked.csv')


def close_batch(client, budget, data_dir, out, targets, results, errors, root, calibration_sha, elapsed):
    try:
        catalog_rows = catalogs(client, (row['tic'] for row in targets))
        atomic_json(out / 'catalog_snapshot.json', catalog_rows)
    except (requests.RequestException, ValueError) as exc:
        catalog_rows = None
        log(f'{out.name}: catalog unavailable: {exc}')
    candidates = []
    for row in results:
        for item in row['candidates']:
            catalog = None if catalog_rows is None else catalog_rows[row['tic']]
            candidates.append({**item, 'tic': row['tic'], 'catalog': match_catalog(item, catalog),
                               'vet': {**item['vet'], 'pixel': {'state': 'INCONCLUSIVE', 'reason': 'Outside pixel review quota'}}})
    candidates.sort(key=ranking, reverse=True)
    top = candidates[:15]
    by_tic = {row['tic']: row for row in results}
    for candidate in top:
        target = by_tic[candidate['tic']]
        add_pixel(client, budget, data_dir, candidate, target)
    candidates.sort(key=ranking, reverse=True)
    for index, candidate in enumerate(candidates[:15], 1):
        target = by_tic[candidate['tic']]
        candidate['diagnostic'] = diagnose(out, data_dir, target, candidate, f'candidate_{index:02d}')
    candidate_outputs(out, candidates)
    ranking_sha = digest(out / 'candidates_ranked.json')
    public_results = [{**row, 'candidates': len(row['candidates'])} for row in results]
    atomic_json(out / 'target_results.json', public_results)
    controls = []
    if out.name == 'batch_001':
        truth = read_json(root / 'blind_key.json')
        for item in truth:
            tic, p = item['tic'], item['period_days']
            matching = [(rank, row) for rank, row in enumerate(candidates, 1) if row['tic'] == tic and
                        row['period_days'] and abs(row['period_days'] / p - 1) <= .005 and
                        abs(phase(row['epoch_btjd'], p, item['epoch_btjd'])) <= duration_days(row) / 2]
            blind = {**item, 'recovered': bool(matching)}
            if matching:
                rank, row = matching[0]
                blind.update(batch_rank=rank, period_error_pct=100 * abs(row['period_days'] / p - 1))
                blind['diagnostic'] = diagnose(out, data_dir, by_tic[tic], row, f'control_{tic}')
            elif tic in by_tic and by_tic[tic]['periodic_peaks']:
                peak = by_tic[tic]['periodic_peaks'][0]
                blind['diagnostic'] = diagnose(out, data_dir, by_tic[tic], {**peak, 'kind': 'periodic'}, f'control_{tic}')
            controls.append(blind)
    progress = {'targets': len(targets), 'processed': len(results), 'errors': errors, 'controls': controls,
                'catalog_available': catalog_rows is not None, 'candidate_count': len(candidates),
                'periodic_count': sum(item['kind'] == 'periodic' for item in candidates),
                'single_raw_count': sum(row['raw_single_count'] for row in results),
                'single_retained_count': sum(item['kind'] == 'single' for item in candidates),
                'ranking_sha256_before_unblind': ranking_sha, 'calibration_report_sha256': calibration_sha,
                'download_budget_charged_bytes': budget.charged, 'elapsed_seconds_this_run': round(elapsed, 2),
                'root_free_bytes': shutil.disk_usage('/').free, 'nvme_free_bytes': shutil.disk_usage(root).free}
    summary(out, progress)
    return progress


def add_pixel(client, budget, directory, candidate, target):
    from astropy.io import fits

    coverage = []
    for item in target['products']:
        with fits.open(directory / item['filename'], memmap=True) as hdus:
            t = hdus[1].data['TIME']
            x = (phase(t, candidate['period_days'], candidate['epoch_btjd']) if candidate['period_days']
                 else t - candidate['epoch_btjd'])
            good = (hdus[1].data['QUALITY'] == 0) & (np.abs(x) < duration_days(candidate) / 2)
            coverage.append((np.count_nonzero(good), item))
    count, item = max(coverage, key=lambda pair: pair[0])
    if count < 20:
        candidate['vet']['pixel'] = {'state': 'INCONCLUSIVE', 'reason': 'No sector with sufficient event coverage'}
        return
    obs = item['filename'].removesuffix('_lc.fits')
    asset = product(item['tic'], item['sector'], obs, 'tp')
    try:
        downloaded = download(client, asset, directory, budget.reserve)
        result = pixel_check(directory / asset['filename'], candidate)
        candidate['vet']['pixel'] = {**result, 'product': downloaded}
    except (OSError, ValueError, KeyError, VerifyError, requests.RequestException, BudgetExceeded) as exc:
        candidate['vet']['pixel'] = {'state': 'INCONCLUSIVE', 'reason': str(exc)[:350]}


def diagnose(out, directory, target, candidate, basename):
    from golden_calibration import plt

    (t, y, _), curves = series(directory, target['products'])
    p = candidate['period_days']
    epoch = candidate['epoch_btjd']
    dur = duration_days(candidate)
    x = phase(t, p, epoch) if p else t - epoch
    near = abs(x) < 2 * dur
    fig, axes = plt.subplots(2, 2, figsize=(11, 6.5))
    overview, folded, detail, local = axes.ravel()
    for raw_t, raw_f, _ in curves:
        overview.scatter(raw_t[::8], (raw_f[::8] / np.median(raw_f) - 1) * 1e6, s=2, color='#a6a8a9')
    overview.scatter(t[::8], y[::8] * 1e6, s=2, color='#2d6579')
    overview.set(xlabel='BTJD', ylabel='Flux (ppm)', title='Raw normalized (gray) and detrended (blue)')
    folded.scatter(x[near] * 24, y[near] * 1e6, s=3, color='#2d6579', alpha=.45)
    folded.axvspan(-12 * dur, 12 * dur, color='#dea263', alpha=.3)
    folded.set(xlabel='Hours from transit', ylabel='Flux (ppm)', title='Phase-folded transit' if p else 'Isolated event')
    if p:
        cycle = np.rint((t[near] - epoch) / p).astype(int)
        for i, color in enumerate(('#2d6579', '#b25b36')):
            group = cycle % 2 == i
            detail.scatter(x[near][group] * 24, y[near][group] * 1e6, s=3, alpha=.45, color=color, label=('Even', 'Odd')[i])
        detail.legend()
        detail.set(xlabel='Hours from transit', ylabel='Flux (ppm)', title='Odd and even transits')
        near_time = t[np.argmin(abs(x))]
        nearest_epoch = epoch + round((near_time - epoch) / p) * p
        event = abs(t - nearest_epoch) < 2 * dur
        local.scatter((t[event] - nearest_epoch) * 24, y[event] * 1e6, s=4, color='#2d6579')
        local.set(xlabel='Hours from single transit', ylabel='Flux (ppm)', title=f'Local event at BTJD {nearest_epoch:.5f}')
    else:
        inside = abs(x[near]) < dur / 2
        profile = np.where(inside, -candidate['depth_ppm'], 0.)
        detail.scatter(x[near] * 24, y[near] * 1e6 - profile, s=4, color='#2d6579')
        detail.axhline(0, color='#a6a8a9')
        detail.set(xlabel='Hours from transit', ylabel='Residual (ppm)', title='Box-template residual')
        evidence = candidate['vet']['pixel']
        if evidence.get('difference_image'):
            local.imshow(evidence['difference_image'], origin='lower', cmap='RdBu_r')
            local.set_title('In/out difference image')
        else:
            local.text(.05, .5, evidence.get('reason', 'Pixel result pending')[:120], transform=local.transAxes, wrap=True)
            local.set_axis_off()
    fig.suptitle(f"TIC {target['tic']} · {p:.6f} d" if p else f"TIC {target['tic']} · BTJD {epoch:.5f}")
    fig.tight_layout()
    filename = f'{basename}.png'
    scratch = out / f'{filename}.part'
    fig.savefig(scratch, dpi=135, format='png')
    plt.close(fig)
    scratch.replace(out / filename)
    return filename


def summary(out, progress):
    summary_path = out / 'batch_summary.md'
    lines = [f'# TESS {out.name}', '',
             f"- 光变处理完成 {progress['processed']} / {progress['targets']} 颗。",
             f"- 本轮耗时 {progress['elapsed_seconds_this_run']:.1f} 秒，下载计账 {progress['download_budget_charged_bytes'] / 1024**3:.3f} GiB。",
             f"- 周期候选 {progress['periodic_count']} 条；单次通道原始触发 {progress['single_raw_count']} 条，去重保留 {progress['single_retained_count']} 条。",
             f"- 根分区可用 {progress['root_free_bytes'] / 1024**3:.1f} GiB，NVMe 可用 {progress['nvme_free_bytes'] / 1024**3:.1f} GiB。",
             f"- PS 与 TOI 交叉核对 {'可用' if progress['catalog_available'] else '待重试'}。"]
    if progress['controls']:
        lines.extend(['', '## 盲测'] + [
            f"- TIC {row['tic']} {row['planet']} {'已恢复' if row['recovered'] else '未恢复'}。" for row in progress['controls']
        ])
    if progress['errors']:
        lines.extend(['', '## 未完成目标'] + [f'- TIC {key} {message}' for key, message in progress['errors'].items()])
    lines.extend(['', '星表未匹配的记录均为待复核凌星候选信号。'])
    scratch = summary_path.with_suffix('.md.part')
    scratch.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    scratch.replace(summary_path)
    atomic_json(out / 'batch_status.json', progress)


def batch(root, client, pool, number, targets, calibration_sha):
    started = time.monotonic()
    name = f'batch_{number:03d}'
    out, directory = root / 'out' / name, root / 'raw' / name
    checkpoint = out / 'checkpoint'
    checkpoint.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    status = out / 'batch_status.json'
    if status.is_file():
        previous = read_json(status)
        if previous['processed'] == len(targets) and previous['catalog_available']:
            return previous
    budget = DownloadBudget(out / 'download_budget.json')
    atomic_json(out / 'input_targets.json', targets)
    results, errors, future_rows = [], {}, {}
    log(f'{name}: begin {len(targets)} targets')
    for target in targets:
        tic = target['tic']
        path = checkpoint / f'{tic}.json'
        if path.is_file():
            results.append(read_json(path))
            continue
        try:
            products = [download(client, row, directory, budget.reserve) for row in target['products']]
            future_rows[pool.submit(process_target, directory, products)] = tic
        except (OSError, ValueError, KeyError, VerifyError, requests.RequestException, BudgetExceeded) as exc:
            errors[tic] = str(exc)[:350]
            log(f'{name}: TIC {tic} download failed: {errors[tic]}')
    for future in as_completed(future_rows):
        tic = future_rows[future]
        try:
            result = future.result()
            atomic_json(checkpoint / f'{tic}.json', result)
            results.append(result)
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            errors[tic] = str(exc)[:350]
            log(f'{name}: TIC {tic} processing failed: {errors[tic]}')
        if len(results) == 1 or len(results) % 10 == 0:
            log(f'{name}: {len(results)}/{len(targets)} checkpointed')
    results.sort(key=lambda row: row['tic'])
    return close_batch(client, budget, directory, out, targets, results, errors, root, calibration_sha,
                       time.monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--total', type=int, default=1000)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--select-only', action='store_true')
    args = parser.parse_args()
    if args.total < args.batch_size or args.batch_size < 100 or args.workers < 1:
        parser.error('This campaign requires at least 100 targets and a positive worker count')
    environment(args.root)
    if args.select_only:
        with session() as client:
            targets = build_manifest(args.root, client, args.total)
        log(f'Frozen target manifest: {len(targets)} unique targets')
        return
    with session() as client, ProcessPoolExecutor(max_workers=args.workers) as pool:
        targets = build_manifest(args.root, client, args.total)
        log(f'Frozen target manifest: {len(targets)} unique targets')
        calibration_sha = wait_for_calibration(args.root)
        log('Expanded golden regression passed production thresholds')
        for offset in range(0, len(targets), args.batch_size):
            number = offset // args.batch_size + 1
            subset = targets[offset:offset + args.batch_size]
            environment(args.root)
            progress = batch(args.root, client, pool, number, subset, calibration_sha)
            if progress['processed'] != len(subset) or not progress['catalog_available']:
                log(f'Batch {number} incomplete; retrying from checkpoints')
                time.sleep(20)
                progress = batch(args.root, client, pool, number, subset, calibration_sha)
            if progress['processed'] != len(subset) or not progress['catalog_available']:
                raise RuntimeError(f'Batch {number} incomplete after retry')
            log(f"Batch {number} complete: {progress['processed']} targets, {progress['candidate_count']} candidate signals")
            if number == 1 and not all(row['recovered'] for row in progress['controls']):
                log('Pilot blind-control recovery incomplete; see the diagnostic plots before interpreting candidates')
            atomic_json(args.root / 'out/campaign_progress.json', {
                'processed': offset + progress['processed'], 'total': args.total, 'last_batch': f'batch_{number:03d}',
                'complete': offset + progress['processed'] == args.total,
            })
    log(f'Campaign complete: {args.total} targets')


if __name__ == '__main__':
    main()
