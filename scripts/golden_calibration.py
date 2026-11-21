"""Run the TESS SPOC golden samples using an ephemeris-independent search."""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path

from astropy.io import fits
from astropy.timeseries import BoxLeastSquares
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import find_peaks


CADENCE = 2 / 1440
RANK_LIMIT = 10
MIN_SNR = 7.5
MIN_SDE = 6.5
MIN_SINGLE_SNR = 5.5


def read_curve(root, item):
    path = root / item['filename']
    if sha256(path.read_bytes()).hexdigest() != item['sha256']:
        raise ValueError(f'Hash mismatch: {path}')
    with fits.open(path, memmap=False) as hdus:
        header, lightcurve = hdus[0].header, hdus[1]
        if (header['TICID'] != item['tic'] or header['SECTOR'] != item['sector']
            or lightcurve.header['TIMESYS'] != 'TDB' or lightcurve.header['BJDREFI'] != 2457000
            or lightcurve.header.get('BJDREFF', 0) != 0):
            raise ValueError(f'Unexpected FITS metadata: {path}')
        table = lightcurve.data
        t, f, e = (np.asarray(table[key], dtype=float) for key in ('TIME', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR'))
        ok = (table['QUALITY'] == 0) & np.isfinite(t) & np.isfinite(f) & np.isfinite(e) & (e > 0)
        if abs(np.median(np.diff(t[ok])) - CADENCE) > 0.05 * CADENCE:
            raise ValueError(f'Unexpected cadence: {path}')
        return t[ok], f[ok], e[ok]


def segments(t):
    return np.split(np.arange(len(t)), np.flatnonzero(np.diff(t) > 5 * CADENCE) + 1)


def detrend(t, f, e):
    usable = []
    for idx in segments(t):
        if len(idx) < 720:
            continue
        flux = f[idx]
        baseline = median_filter(flux, size=721, mode='reflect')
        residual = flux / baseline - 1
        center = np.median(residual)
        scatter = 1.4826 * np.median(np.abs(residual - center))
        ok = residual <= center + 5 * scatter
        usable.append((t[idx][ok], residual[ok], e[idx][ok] / baseline[ok]))
    if not usable:
        raise ValueError('No contiguous segments long enough to detrend')
    return tuple(np.concatenate(arrays) for arrays in zip(*usable))


def periodic_inputs(t, y, dy):
    """Limit isolated artifacts and use measured scatter as the BLS error floor."""
    flux, errors = y.copy(), dy.copy()
    boundaries = np.flatnonzero(np.diff(t) > 1) + 1
    for indices in np.split(np.arange(len(t)), boundaries):
        if len(indices) < 30:
            continue
        samples = y[indices]
        median = np.median(samples)
        sigma = max(1.4826 * np.median(np.abs(samples - median)), np.median(dy[indices]))
        flux[indices] = np.clip(samples - median, -6 * sigma, 6 * sigma)
        errors[indices] = np.maximum(dy[indices], sigma)
    return flux, errors


def periodic_search(t, y, dy):
    model = BoxLeastSquares(t, *periodic_inputs(t, y, dy))
    span = np.ptp(t)
    durations = np.asarray([1, 1.5, 2, 3, 4, 6, 8]) / 24
    step = 0.25 * durations.min() / span
    grid = np.geomspace(0.8, 30, int(np.ceil(np.log(30 / 0.8) / step)) + 1)
    result = model.power(grid, durations, objective='snr', oversample=8)
    power = result.power
    center, spread = np.mean(power), np.std(power)
    peaks = find_peaks(power)[0]
    peaks = np.unique(np.append(peaks, np.argmax(power)))
    candidates = []
    for i in peaks[np.argsort(power[peaks])[::-1]]:
        period, epoch, duration = (float(arr[i]) for arr in (result.period, result.transit_time, result.duration))
        width = duration * period / span
        if any(abs(period - known['period_days']) <= max(width, known['duration_days'] * known['period_days'] / span)
               for known in candidates):
            continue
        inside = np.abs(phase(t, period, epoch)) <= duration / 2
        _, counts = np.unique(np.rint((t[inside] - epoch) / period), return_counts=True)
        events = int(np.count_nonzero(counts >= 0.5 * duration / CADENCE))
        if events < 2:
            continue
        candidates.append({'rank': len(candidates) + 1, 'period_days': period, 'epoch_btjd': epoch,
                           'duration_days': duration, 'depth_ppm': float(result.depth[i]) * 1e6,
                           'snr': float(result.depth_snr[i]), 'sde': float((power[i] - center) / spread),
                           'covered_events': events})
        if len(candidates) == RANK_LIMIT:
            break
    return candidates


def phase(t, period, epoch):
    return (t - epoch + period / 2) % period - period / 2


def single_search(t, y):
    peaks = []
    for idx in segments(t):
        ts, ys = t[idx], y[idx]
        total = np.r_[0., ys.cumsum()]
        squares = np.r_[0., (ys * ys).cumsum()]
        for hours in (2, 4, 8):
            half = hours / 48
            a, b, c, d = [np.searchsorted(ts, ts + shift * half) for shift in (-3, -1, 1, 3)]
            inside, outside = c - b, b - a + d - c
            valid = (inside >= .75 * 2 * half / CADENCE) & (b-a >= .75 * 2 * half / CADENCE) & (d-c >= .75 * 2 * half / CADENCE)
            v = np.flatnonzero(valid)
            if not len(v):
                continue
            side = total[b] - total[a] + total[d] - total[c]
            mean = side[v] / outside[v]
            variance = (squares[b[v]] - squares[a[v]] + squares[d[v]] - squares[c[v]]) / outside[v] - mean**2
            scatter = 1.4826 * np.median(np.abs(ys - np.median(ys)))
            noise = np.sqrt(np.maximum(variance, scatter**2) * (1 / inside[v] + 1 / outside[v]))
            depth = mean - (total[c[v]] - total[b[v]]) / inside[v]
            snr = np.zeros(len(ts))
            snr[v] = np.divide(depth, noise, out=np.zeros_like(depth), where=noise > 0)
            for i in find_peaks(snr, height=MIN_SINGLE_SNR, distance=round(2 * half / CADENCE))[0]:
                score_index = np.searchsorted(v, i)
                peaks.append({'epoch_btjd': float(ts[i]), 'duration_hours': hours, 'snr': float(snr[i]),
                              'depth_ppm': float(depth[score_index] * 1e6)})
    selected = []
    for event in sorted(peaks, key=lambda item: item['snr'], reverse=True):
        if not any(abs(event['epoch_btjd'] - other['epoch_btjd']) <=
                   (event['duration_hours'] + other['duration_hours']) / 48 for other in selected):
            selected.append(event)
    return selected


def compare(candidates, references):
    matches = []
    for candidate in candidates:
        p, epoch = candidate['period_days'], candidate['epoch_btjd']
        for ref in references:
            expected = ref['epoch_btjd'] + round((epoch - ref['epoch_btjd']) / ref['period_days']) * ref['period_days']
            rel_error = abs(p / ref['period_days'] - 1)
            timing = abs(expected - epoch)
            if rel_error <= .005 and timing <= candidate['duration_days'] / 2:
                matches.append({**candidate, 'planet': ref['planet'], 'relative_period_error': rel_error,
                                'epoch_error_days': timing,
                                'passed': candidate['snr'] >= MIN_SNR and candidate['sde'] >= MIN_SDE})
    return matches


def plot_folded(out, tic, t, y, candidate):
    x = phase(t, candidate['period_days'], candidate['epoch_btjd']) * 24
    duration = candidate['duration_days'] * 24
    keep = np.abs(x) < 3 * duration
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.scatter(x[keep], 1e6 * y[keep], s=3, alpha=.3, color='#31576f', rasterized=True)
    ax.axvspan(-duration / 2, duration / 2, alpha=.14, color='#ce7730')
    ax.set(xlabel='Hours from transit center', ylabel='Relative flux (ppm)',
           title=f"TIC {tic}  •  {candidate['period_days']:.6f} d")
    fig.tight_layout()
    fig.savefig(out / f'TIC{tic}_folded.png', dpi=160)
    plt.close(fig)


def injection_check(curve):
    t, f, e = curve
    longest = max(segments(t), key=len)
    duration = 4 / 24
    epoch = t[longest[len(longest) // 2]]
    baseline_t, baseline_y, _ = detrend(t, f, e)
    baseline = single_search(baseline_t, baseline_y)
    depth = 8 * 1.4826 * np.median(np.abs(baseline_y - np.median(baseline_y)))
    injected = f * (1 - depth * (np.abs(t - epoch) < duration / 2))
    recovered = single_search(*detrend(t, injected, e)[:2])
    subsequent = [item for item in recovered if abs(item['epoch_btjd'] - epoch) < duration / 4]
    preexisting = [item for item in baseline if abs(item['epoch_btjd'] - epoch) < duration / 4]
    edge = t[longest[-1]] + CADENCE
    edge_flux = f * (1 - depth * (np.abs(t - edge) < duration / 4))
    edge_events = single_search(*detrend(t, edge_flux, e)[:2])
    edge_triggered = any(abs(item['epoch_btjd'] - edge) < duration / 2 for item in edge_events)
    return {'injected_epoch_btjd': float(epoch), 'duration_hours': 4, 'depth_ppm': float(depth * 1e6),
            'matched_events': subsequent, 'preexisting_events': preexisting, 'baseline_event_count': len(baseline),
            'edge_injection_btjd': float(edge), 'edge_triggered': edge_triggered,
            'passed': bool(subsequent and not preexisting and not edge_triggered)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((args.input / 'tess-golden-inputs.json').read_text(encoding='utf-8'))
    by_tic = defaultdict(list)
    for item in manifest:
        if item['filename'].endswith('_lc.fits'):
            by_tic[item['tic']].append(item)
    if set(by_tic) != {261136679, 150428135} or len(by_tic[150428135]) < 3:
        raise ValueError('Golden light-curve manifest must contain both targets and at least 3 TOI-700 sectors')
    report = {'search_code_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
              'bls_preprocessing': 'Local median, robust scatter error floor, symmetric six-sigma clipping for BLS only',
              'period_search_days': [0.8, 30], 'rank_limit': RANK_LIMIT, 'min_snr': MIN_SNR,
              'min_sde': MIN_SDE, 'min_single_snr': MIN_SINGLE_SNR, 'targets': []}
    for tic, items in by_tic.items():
        items.sort(key=lambda item: item['sector'])
        curves = [read_curve(args.input, item) for item in items]
        t, y, dy = (np.concatenate(arrays) for arrays in zip(*(detrend(*curve) for curve in curves)))
        candidates = periodic_search(t, y, dy)
        report['targets'].append({'tic': tic, 'sectors': [item['sector'] for item in items],
                                  'clean_points': len(t), 'candidates': candidates})
        print(f'TIC {tic}: BLS completed, {len(t)} cadences', flush=True)
        if tic == 261136679:
            report['single_injection'] = injection_check(curves[0])
        report['targets'][-1]['_plot_arrays'] = (t, y)
    # Read the answer key after every search has completed.
    refs = json.loads((args.input / 'golden-ephemerides.json').read_text(encoding='utf-8'))
    for target in report['targets']:
        target['matches'] = compare(target['candidates'], refs[str(target['tic'])])
        target['passed'] = any(match['passed'] for match in target['matches'])
        t, y = target.pop('_plot_arrays')
        choices = [match for match in target['matches'] if match['passed']] or target['candidates']
        if choices:
            plot_folded(args.output, target['tic'], t, y, choices[0])
    report['passed'] = report['single_injection']['passed'] and all(target['passed'] for target in report['targets'])
    (args.output / 'golden_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
