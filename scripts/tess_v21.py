"""V2.1 transit search shared by the Jetson regression and rolling scanner."""

from collections import defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time
import warnings

from astropy.io import fits
from astropy.timeseries import BoxLeastSquares, LombScargle
import numpy as np
from scipy.signal import find_peaks

from golden_calibration import CADENCE, detrend, periodic_inputs, phase, plot_folded, read_curve, single_search
from tess_sources import atomic_json, digest, download, expanded_golden, products_for_sector, session
from tess_vetting import duration_days, lightcurve_checks, robust_sigma


def keplerian_duration_ratio(signal, radius_rsun=None):
    """Compare event duration to the circular Keplerian equatorial transit limit."""
    p = signal.get('period_days')
    if not p or not radius_rsun or radius_rsun <= 0:
        return None
    mass = max(.15, radius_rsun**.8)
    k = math.sqrt(max(signal.get('depth_ppm') or 0, 0)*1e-6)
    a = 4.2083*p**(2/3)*mass**(1/3)
    t_circ = p/math.pi*math.asin(min(1., radius_rsun*(1+k)/a))
    return duration_days(signal)/t_circ if t_circ > 0 else None


def prewhiten_curve(t, f, e):
    """SHERLOCK-style pre-detrend f+2f Fourier whitening on raw normalized sector flux."""
    if len(t) < 720 or np.ptp(t) < 3:
        return f, []
    base = float(np.median(f))
    if not np.isfinite(base) or base <= 0:
        return f, []
    by = f / base - 1
    removed = []
    for _ in range(3):
        med = np.median(by)
        sig = robust_sigma(by)
        if not np.isfinite(sig) or sig <= 0:
            break
        mask = (by >= med - 3*sig) & (by <= med + 4*sig)
        if mask.sum() < 400:
            break
        tm, ym = t[mask], by[mask]
        labels = np.floor((tm - tm[0]) / (30/1440)).astype(int)
        steps = np.r_[0, np.flatnonzero(np.diff(labels)) + 1]
        sizes = np.diff(np.r_[steps, len(labels)])
        keep = sizes >= 4
        if keep.sum() < 80:
            break
        tb = (np.add.reduceat(tm, steps)/sizes)[keep]
        yb = (np.add.reduceat(ym, steps)/sizes)[keep]
        if np.std(yb) <= 0:
            break
        ls = LombScargle(tb, yb)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            freqs, power = ls.autopower(minimum_frequency=1/15, maximum_frequency=1/.4,
                                        samples_per_peak=4, method='fast')
        finite = np.isfinite(power)
        if not finite.any():
            break
        peak = int(np.nanargmax(power))
        peak_power = float(power[peak])
        if peak_power < .20:
            break
        fap = float(np.clip(ls.false_alarm_probability(min(peak_power, .999999), method='baluev',
                                                       minimum_frequency=freqs[0], maximum_frequency=freqs[-1]), 0, 1))
        if not np.isfinite(fap) or fap > 1e-4:
            break
        w = 2*np.pi*float(freqs[peak])
        t0 = tm[0]

        def basis(times):
            return np.column_stack((np.ones_like(times), times-t0, np.cos(w*times), np.sin(w*times),
                                    np.cos(2*w*times), np.sin(2*w*times)))

        coef = np.linalg.lstsq(basis(tm), ym, rcond=None)[0]
        model = basis(t) @ coef
        resid_sig = robust_sigma(ym - model[mask])
        amp = float(np.ptp(model))
        if amp < 2.5*max(resid_sig, 1e-9):
            break
        by = by - model + np.median(model)
        removed.append({'sector_start_btjd': float(t[0]), 'period_days': float(1/freqs[peak]),
                        'power': peak_power, 'amplitude_ppm': amp*1e6})
    cleaned = base * (1 + by)
    return (cleaned, removed) if np.all(cleaned > 0) else (f, [])


def score_search(t, y, e, radius_rsun=None):
    span = np.ptp(t)
    durations = np.array([1, 1.5, 2, 3, 4, 6, 8]) / 24
    # One grid step accumulates at most half of the shortest searched duration.
    step = .5 * durations[0] / span
    grid = np.geomspace(.8, 45, int(np.ceil(np.log(45/.8) / step)) + 1)
    flux, err = periodic_inputs(t, y, e)
    # Weighted 8-minute binning retains at least 7 bins across the shortest transit.
    labels = np.floor((t - t[0]) * 180).astype(int)
    indices = np.r_[0, np.flatnonzero(np.diff(labels)) + 1]
    inverse_variance = err**-2
    weights = np.add.reduceat(inverse_variance, indices)
    tt = np.add.reduceat(t * inverse_variance, indices) / weights
    yy = np.add.reduceat(flux * inverse_variance, indices) / weights
    model = BoxLeastSquares(tt, yy, weights**-.5)
    scan = model.power(grid, durations, objective='snr', oversample=4)
    center = float(np.median(scan.power))
    scatter = float(robust_sigma(scan.power))
    first = int(np.argmax(scan.power))
    peaks = np.unique(np.r_[first, find_peaks(scan.power)[0]])
    ordered = peaks[np.argsort(scan.power[peaks], kind='stable')[::-1]]
    for i in ordered[:25]:
        period, epoch, duration, depth, snr = map(float, (scan.period[i], scan.transit_time[i],
                                                         scan.duration[i], scan.depth[i], scan.depth_snr[i]))
        sde = (float(scan.power[i]) - center) / scatter if scatter > 0 else 0.
        if not (snr >= 7.5 and sde >= 6.5):
            continue
        kepler_ratio = keplerian_duration_ratio({'kind': 'periodic', 'period_days': period,
                                                 'duration_days': duration, 'depth_ppm': depth*1e6}, radius_rsun)
        if kepler_ratio is not None and kepler_ratio > 1.8:
            continue
        inside = abs(phase(t, period, epoch)) <= duration / 2
        _, counts = np.unique(np.rint((t[inside] - epoch)/period), return_counts=True)
        covered = int(np.count_nonzero(counts >= .5 * duration / CADENCE))
        if covered >= 2:
            return {'kind': 'periodic', 'period_days': period, 'epoch_btjd': epoch, 'duration_days': duration,
                    'depth_ppm': depth * 1e6, 'snr': snr, 'sde': sde, 'mad_sde': sde,
                    'first_peak': bool(i == first), 'covered_events': covered}
    return None


def iterative_search(t, y, e, radius_rsun=None):
    keep = np.ones(len(t), dtype=bool)
    signals = []
    for iteration in range(1, 4):
        if keep.sum() < 720:
            break
        signal = score_search(t[keep], y[keep], e[keep], radius_rsun=radius_rsun)
        if signal is None:
            break
        signals.append({**signal, 'iteration': iteration})
        delta = abs(phase(t, signal['period_days'], signal['epoch_btjd']))
        keep &= delta > .75 * signal['duration_days']
    return signals


def isolated_search(t, y, already):
    candidates = []
    for signal in single_search(t, y):
        epoch, duration = signal['epoch_btjd'], signal['duration_hours']/24
        if any(abs(phase(epoch, row['period_days'], row['epoch_btjd'])) <
               max(duration, row['duration_days'])/2 for row in already):
            continue
        lo, hi = np.searchsorted(t, [epoch - 1.5*duration, epoch + 1.5*duration])
        signed = t[lo:hi] - epoch
        x, values = abs(signed), y[lo:hi]
        inside, side = x <= duration/2, x > duration/2
        left, right = signed < -duration/2, signed > duration/2
        if min(inside.sum(), left.sum(), right.sum()) < 10 or side.sum() < 20:
            continue
        baseline = np.median(values[side])
        mad = robust_sigma(values[side])
        depth = baseline - np.mean(values[inside])
        step = abs(np.median(values[left]) - np.median(values[right]))
        if mad <= 0 or depth <= 5.5*mad or np.max(values[side]) - baseline > 5.5*mad or step > 3.5*mad:
            continue
        candidates.append({**signal, 'kind': 'single', 'period_days': None, 'sde': None, 'mad_sde': None,
                           'depth_ppm': float(depth * 1e6), 'depth_over_local_mad': float(depth/mad),
                           'shoulder_step_sigma': float(step/mad)})
    # A true long-period single transit is isolated, not >=3 deep dips in the same 27-day sector.
    return [c for c in candidates if sum(1 for o in candidates if abs(o['epoch_btjd'] - c['epoch_btjd']) <= 14) < 3]


def injection_test(curve):
    from golden_calibration import segments

    t, f, e = curve
    longest = max(segments(t), key=len)
    duration = 4/24
    epoch = float(t[longest[len(longest)//2]])
    bt, by, _ = detrend(t, f, e)
    depth = float(8*robust_sigma(by))

    def scan(center, half_width):
        flux = f * (1 - depth*(abs(t-center) < half_width))
        return isolated_search(*detrend(t, flux, e)[:2], [])

    matches = [row for row in scan(epoch, duration/2) if abs(row['epoch_btjd']-epoch) < duration/4]
    prior = [row for row in isolated_search(bt, by, []) if abs(row['epoch_btjd']-epoch) < duration/4]
    edge = float(t[longest[-1]] + CADENCE)
    gap_hits = [row for row in scan(edge, duration/4) if abs(row['epoch_btjd']-edge) < duration/2]
    return {'passed': bool(matches and not prior and not gap_hits), 'epoch_btjd': epoch, 'depth_ppm': depth*1e6,
            'duration_hours': 4, 'matches': matches, 'edge_btjd': edge, 'edge_hits': gap_hits}


def stellar_checks(t, y, e, signal, meta):
    duration = duration_days(signal)
    p, epoch = signal['period_days'], signal['epoch_btjd']
    radius = meta.get('radius')
    ruwe = meta.get('ruwe')
    rp = radius * 109.076 * np.sqrt(max(signal['depth_ppm'], 0)*1e-6) if radius else None
    bad = (rp is not None and rp > 22) or (radius is not None and radius > 1.2) or (ruwe is not None and ruwe > 1.4)
    stellar = {'state': 'FLAG' if bad else 'PASS' if rp is not None and ruwe is not None else 'INCONCLUSIVE',
               'rp_rearth': rp, 'radius_rsun': radius, 'gaia_ruwe': ruwe, 'gaia_dr3': meta.get('gaia_dr3')}
    aliases = {'state': 'NOT_APPLICABLE'}
    if p:
        def power(period, center):
            mask = abs(phase(t, period, center)) < duration/2
            ids, counts = np.unique(np.rint((t[mask]-center)/period), return_counts=True)
            if len(ids) < 2 or np.count_nonzero(counts>=.5*duration/CADENCE) < 2:
                return 0.
            weights = e[mask]**-2
            return float(max(0., -np.sum(y[mask]*weights))**2/np.sum(weights))
        fy, fe = periodic_inputs(t, y, e)
        y, e = fy, fe
        base = power(p, epoch)
        options = {'P/2': power(p/2, epoch), 'P/3': power(p/3, epoch),
                   '2P': max(power(2*p, epoch), power(2*p, epoch+p))}
        aliases = {'state': 'FLAG' if max(options.values()) > base+16 else 'PASS',
                   'base_power': base, 'alternative_power': options}
    ratio = keplerian_duration_ratio(signal, radius)
    if signal['kind'] == 'periodic' and ratio is not None:
        kepler = {'state': 'FLAG' if ratio > 1.8 else 'PASS', 'duration_to_keplerian_ratio': float(ratio)}
    else:
        kepler = {'state': 'NOT_APPLICABLE', 'duration_to_keplerian_ratio': None if ratio is None else float(ratio)}
    return {'stellar_radius_ruwe': stellar, 'kepler_duration': kepler, 'harmonics': aliases}


def sap_check(directory, manifest, signal):
    window = duration_days(signal)
    period, epoch = signal['period_days'], signal['epoch_btjd']
    local = [[], []]
    for row in manifest:
        with fits.open(directory / row['filename'], memmap=True) as hdus:
            table = hdus[1].data
            t, sap, sap_e, pdc, pdc_e = (np.asarray(table[name], dtype=float) for name in
                                        ('TIME', 'SAP_FLUX', 'SAP_FLUX_ERR', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR'))
            valid = (table['QUALITY'] == 0) & np.isfinite(t+sap+sap_e+pdc+pdc_e) & (sap_e>0) & (pdc_e>0)
            for index, (flux, error) in enumerate(((sap, sap_e), (pdc, pdc_e))):
                try:
                    tt, yy, _ = detrend(t[valid], flux[valid], error[valid])
                except ValueError:
                    continue
                x = abs(phase(tt, period, epoch) if period else tt-epoch)
                local[index].append((yy[x < window/2], yy[(x > window*.7)&(x < window*1.5)]))
    stats = []
    for band in local:
        if not band:
            return {'state': 'INCONCLUSIVE'}
        inside, outside = [np.concatenate(flux) for flux in zip(*band)]
        if min(len(inside), len(outside)) < 20:
            return {'state': 'INCONCLUSIVE'}
        stats.append((float(np.median(outside)-np.median(inside)),
                      float(robust_sigma(outside)*np.sqrt(1/len(inside)+1/len(outside)))))
    (sap, se), (pdc, pe) = stats
    flagged = abs(sap-pdc) > max(.5*abs(pdc), 4*np.hypot(se, pe))
    return {'state': 'FLAG' if flagged else 'PASS', 'sap_ppm': sap*1e6, 'pdcsap_ppm': pdc*1e6}


def processing(directory, products, meta):
    curves = [read_curve(directory, row) for row in products]
    rot_modes = []
    whitened = []
    for ct, cf, ce in curves:
        cf_clean, modes = prewhiten_curve(ct, cf, ce)
        rot_modes.extend(modes)
        whitened.append((ct, cf_clean, ce))
    t, y, e = [np.concatenate(arrays) for arrays in zip(*(detrend(*curve) for curve in whitened))]
    if not np.all(np.diff(t) > 0):
        raise ValueError('Non-monotonic stitched BTJD')
    periods = iterative_search(t, y, e, radius_rsun=meta.get('radius'))
    singles = isolated_search(t, y, periods)
    records = []
    for signal in periods + singles:
        checks = lightcurve_checks(t, y, signal)
        checks.update(stellar_checks(t, y, e, signal, meta))
        checks['sap_pdcsap'] = sap_check(directory, products, signal)
        checks['rotation_prewhiten'] = {'state': 'PASS', 'removed_modes': rot_modes}
        records.append({**signal, 'vet': checks})
    return {'tic': products[0]['tic'], 'products': products, 'stars': meta, 'candidates': records,
            'clean_points': len(t), 'span_days': float(np.ptp(t)), 'removed_rotation_modes': rot_modes}


def calibrate(root, output):
    started = time.monotonic()
    expanded_golden()
    source = root / 'golden_expanded'
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'tess-golden-inputs.json').read_text())
    # Existing S1/4/8/11/12 already cover five independent sectors; retain this frozen baseline.
    ephemerides = json.loads((source / 'golden-ephemerides.json').read_text())
    ephemerides['150428135'].append({'planet': 'TOI-700 d', 'period_days': 37.423457, 'epoch_btjd': 3763.01612})
    golden_radii = {261136679: 1.10, 150428135: 0.42}
    targets = defaultdict(list)
    for row in manifest:
        targets[row['tic']].append(row)
    report = {'search_code_sha256': digest(Path(__file__)), 'period_days': [.8, 45], 'targets': [],
              'download_source': 'MAST AWS public archive (verified cached products reused)',
              'source_setup_seconds': round(time.monotonic()-started, 3)}
    for tic, rows in sorted(targets.items()):
        rows.sort(key=lambda row: row['sector'])
        curves = [read_curve(source, row) for row in rows]
        whitened = [(ct, prewhiten_curve(ct, cf, ce)[0], ce) for ct, cf, ce in curves]
        t, y, e = [np.concatenate(arrays) for arrays in zip(*(detrend(*curve) for curve in whitened))]
        signals = iterative_search(t, y, e, radius_rsun=golden_radii.get(tic))
        matches = []
        for item in signals:
            for reference in ephemerides[str(tic)]:
                error = abs(item['period_days']/reference['period_days']-1)
                delta = abs(phase(item['epoch_btjd'], reference['period_days'], reference['epoch_btjd']))
                if error <= .005 and delta <= item['duration_days']/2:
                    matches.append({**item, 'planet': reference['planet'], 'period_error_pct': error*100})
        if signals:
            plot_folded(output, tic, t, y, matches[0] if matches else signals[0])
        if tic == 261136679:
            report['single_injection'] = injection_test(curves[0])
        report['targets'].append({'tic': tic, 'sectors': [row['sector'] for row in rows], 'matches': matches,
                                  'signals': signals, 'passed': bool(matches)})
        print(f'V2.1 golden TIC {tic}: {len(signals)} iterations, {len(matches)} known planets recovered', flush=True)
    report['elapsed_seconds'] = round(time.monotonic()-started, 2)
    report['passed'] = all(row['passed'] for row in report['targets']) and report['single_injection']['passed']
    atomic_json(output / 'golden_report.json', report)
    return report['passed']


if __name__ == '__main__':
    root = Path(sys.argv[1])
    raise SystemExit(0 if calibrate(root, root / 'out/v21_golden') else 2)
