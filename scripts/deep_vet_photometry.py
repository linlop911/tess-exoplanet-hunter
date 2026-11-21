"""Event-local artifact screening, masked Lomb–Scargle and transit shape checks."""

from dataclasses import dataclass, replace
import json
import math
import warnings

from astropy.io import fits
from astropy.timeseries import LombScargle
import numpy as np

from golden_calibration import CADENCE, phase
from tess_vetting import duration_days, robust_sigma


EDGE_DAYS = .5
GAP_DAYS = .2
DUMP_WINDOW = .1
DESAT_FLAG = 32


@dataclass
class Curve:
    sector: int
    time: np.ndarray
    quality: np.ndarray
    flux: np.ndarray
    error: np.ndarray
    background: np.ndarray
    source: object


def load_curve(path):
    with fits.open(path, memmap=True) as hdus:
        hdu = hdus[1]
        if hdu.header.get('BJDREFI') != 2457000 or hdu.header.get('TIMESYS') != 'TDB':
            raise ValueError(f'{path.name}: expected BJD_TDB - 2457000')
        data = hdu.data
        time, quality, flux, error, background = [np.array(data[key], dtype=float if key != 'QUALITY' else np.int64)
                                                  for key in ('TIME', 'QUALITY', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR', 'SAP_BKG')]
        valid = np.isfinite(time)
        if valid.sum() < 50 or np.any(np.diff(time[valid]) <= 0):
            raise ValueError(f'{path.name}: invalid finite time series')
        baseline = np.nanmedian(flux[valid & (quality == 0)])
        if not np.isfinite(baseline) or baseline <= 0:
            raise ValueError(f'{path.name}: invalid PDCSAP median')
        return Curve(hdus[0].header['SECTOR'], time[valid], quality[valid], flux[valid]/baseline-1,
                     error[valid]/baseline, background[valid], path)


def quality_mask(curve):
    return ((curve.quality == 0) & np.isfinite(curve.flux) & np.isfinite(curve.error) & (curve.error > 0))


def centers(curve, candidate, extent=0):
    p = candidate['period_days']
    epoch = candidate['epoch_btjd']
    start, stop = curve.time[0]-extent, curve.time[-1]+extent
    if p is None:
        return [(0, epoch)] if start <= epoch <= stop else []
    return [(i, epoch+i*p) for i in range(int(np.ceil((start-epoch)/p)), int(np.floor((stop-epoch)/p))+1)]


def robust_trend(t, y, side):
    good = side & np.isfinite(t) & np.isfinite(y)
    if good.sum() < 8:
        return None
    # Fit the local shoulders. Clipping only affects the baseline, not the event window.
    for _ in range(2):
        if good.sum() < 8:
            return None
        fit = np.polyfit(t[good], y[good], 1)
        residual = y - np.polyval(fit, t)
        sigma = robust_sigma(residual[good])
        if sigma > 0:
            good &= abs(residual - np.median(residual[good])) <= 4*sigma
    return residual


def bin_values(t, y, width):
    order = np.argsort(t)
    t, y = t[order], y[order]
    labels = np.floor((t-t[0])/width).astype(int)
    steps = np.r_[0, np.flatnonzero(np.diff(labels)) + 1]
    sizes = np.diff(np.r_[steps, len(labels)])
    return np.add.reduceat(t, steps)/sizes, np.add.reduceat(y, steps)/sizes, sizes


def measure_event(curve, candidate, center, cycle, gap_edges, dumps, good):
    duration = duration_days(candidate)
    span = min(1.5*duration, .44*candidate['period_days']) if candidate['period_days'] else 1.5*duration
    lo, hi = np.searchsorted(curve.time, (center-span, center+span))
    t = curve.time[lo:hi]-center
    valid = good[lo:hi]
    inside = abs(t) <= duration/2
    side = abs(t) >= .66*duration
    expected = duration/CADENCE
    near_edge = bool(min(abs(center-curve.time[0]), abs(center-curve.time[-1])) <= EDGE_DAYS)
    near_gap = bool(np.any(abs(gap_edges-center) <= EDGE_DAYS))
    near_dump = bool(np.any(abs(dumps-center) <= max(DUMP_WINDOW, duration/2)))
    count = int((valid & inside).sum())
    raw_in = int(inside.sum())
    if not raw_in and not (near_edge or near_gap) and not np.any(abs(gap_edges-center) < duration):
        return None, None
    left_side = (t <= -.66*duration) & valid
    right_side = (t >= .66*duration) & valid
    coverage = count/expected
    incomplete = bool(coverage < .75 or
                      min(int(left_side.sum()), int(right_side.sum())) < max(6, int(.25*expected)))
    # Planet Hunters TESS lesson (e.g. TIC 100796276): mask edge/dump events only when the event is degraded.
    event = {'sector': curve.sector, 'cycle': cycle, 'btjd': center,
             'sector_edge': near_edge and incomplete, 'downlink_edge': near_gap and incomplete,
             'momentum_dump': near_dump and incomplete, 'near_edge': near_edge, 'near_gap': near_gap,
             'near_dump': near_dump, 'edge_recovered': False, 'in_cadences': count, 'raw_in_cadences': raw_in,
             'coverage_fraction': coverage, 'depth_ppm': None, 'depth_error_ppm': None, 'snr': None,
             'background_peak_sigma': None, 'background_spike': False, 'background_measurable': False}
    if not len(t):
        return event, None
    b = curve.background[lo:hi]
    background_valid = np.isfinite(b) & ((curve.quality[lo:hi] & ~DESAT_FLAG) == 0)
    bin_background = robust_trend(t, b, side & background_valid)
    if bin_background is not None and (inside & background_valid).sum() >= 5:
        use_side = side & background_valid
        sigma = robust_sigma(bin_background[use_side])
        if sigma > 0:
            samples = bin_background[inside & background_valid] - np.median(bin_background[use_side])
            maximum = np.max(np.convolve(samples, np.ones(3)/3, mode='valid'))
            event.update(background_measurable=True, background_peak_sigma=float(maximum/sigma),
                         background_spike=bool(maximum >= 6*sigma))
    if count < max(8, .4*expected) or (side & valid).sum() < 10:
        return event, None
    use_side = side & valid
    y = robust_trend(t, curve.flux[lo:hi], use_side)
    if y is None:
        return event, None
    _, side_bin, sizes = bin_values(t[use_side], y[use_side], 20/1440)
    enough = sizes >= 3
    red = robust_sigma(side_bin[enough]) if enough.sum() >= 4 else 0
    nominal = np.median(curve.error[lo:hi][inside & valid])
    # All three terms are per-2-minute-cadence scatter; combine with the real cadence counts.
    sigma = max(robust_sigma(y[use_side]), nominal, red)
    if (near_edge or near_gap or near_dump) and not incomplete:
        raw = curve.flux[lo:hi]
        step = abs(np.median(raw[left_side]) - np.median(raw[right_side]))
        step_error = sigma*np.sqrt(1/max(1, int(left_side.sum())) + 1/max(1, int(right_side.sum())))
        bad = bool(step > 3.5*max(sigma, step_error) or event['background_spike'])
        event.update(sector_edge=near_edge and bad, downlink_edge=near_gap and bad,
                     momentum_dump=near_dump and bad, edge_recovered=not bad)
    depth = float(np.median(y[use_side]) - np.mean(y[inside & valid]))
    error = float(sigma*np.sqrt(1/max(1, count) + 1/max(1, int(use_side.sum()))))
    event.update(depth_ppm=depth*1e6, depth_error_ppm=error*1e6, snr=depth/error)
    return event, {'t': t[valid], 'flux': y[valid], 'bkg': None if bin_background is None else bin_background[valid]}


def combined(events):
    if not events:
        return {'depth_ppm': None, 'snr': 0., 'events': 0}
    depth = np.array([row['depth_ppm'] for row in events])
    err = np.array([row['depth_error_ppm'] for row in events])
    weights = err**-2
    average = float(np.average(depth, weights=weights))
    # Inflate the uncertainty if the event depths disagree more than their measured noise.
    rms = np.sqrt(np.sum(weights * (depth - average)**2)/max(1, len(depth)-1))
    return {'depth_ppm': average, 'snr': float(average*np.sqrt(weights.sum())/max(1., rms)), 'events': len(depth)}


def shape_fit(events, windows, duration):
    sample = [windows[(event['sector'], event['cycle'])] for event in events]
    if not sample:
        return {'delta_bic': None, 'model': None}
    t = np.concatenate([row['t']/duration for row in sample])
    y = np.concatenate([row['flux'] for row in sample])
    labels = np.linspace(-1.5, 1.5, 37)
    indices = np.digitize(t, labels)-1
    grid, values, spread = [], [], []
    for index in range(36):
        bin_flux = y[indices == index]
        if len(bin_flux) >= 6:
            grid.append((labels[index]+labels[index+1])/2)
            values.append(np.median(bin_flux))
            spread.append(robust_sigma(bin_flux)/np.sqrt(max(1., len(bin_flux)/10)))
    x, f, e = map(np.asarray, (grid, values, spread))
    if len(f) < 18 or not np.any(e > 0):
        return {'delta_bic': None, 'model': None}
    e = np.maximum(e, max(np.median(e[e > 0])*.5, 1e-9))
    scores = {}
    nlog = np.log(len(x))
    continua = [np.vander(x, order+1, increasing=True) for order in (2, 4)]
    null_bics = []
    for design in continua:
        params = np.linalg.lstsq(design/e[:, None], f/e, rcond=None)[0]
        null_bics.append(float(np.sum(((f-design@params)/e)**2)+design.shape[1]*nlog))
    smooth_u = None
    for model, factors in (('U', (.12, .20, .27)), ('V', (.45, .5))):
        best = None
        for width in (.50, .65, .8, 1., 1.2, 1.4):
            for shift in (-.14, -.07, 0., .07, .14):
                for ingress in factors:
                    template = np.clip((.5*width - abs(x-shift))/(width*ingress), 0, 1)
                    design = np.column_stack((np.ones(len(x)), -template))
                    base, depth = np.linalg.lstsq(design/e[:, None], f/e, rcond=None)[0]
                    if depth <= 0:
                        continue
                    score = float(np.sum(((f - base + depth*template)/e)**2))
                    if best is None or score < best['chi_square']:
                        best = {'chi_square': score, 'width_fraction': width, 'offset_fraction': shift, 'ingress': ingress,
                                'depth_ppm': float(depth*1e6), 'baseline': float(base)}
                    if model == 'U':
                        for continuation in continua:
                            u_design = np.column_stack((continuation, -template))
                            param = np.linalg.lstsq(u_design/e[:, None], f/e, rcond=None)[0]
                            if param[-1] <= 0:
                                continue
                            bic = float(np.sum(((f-u_design@param)/e)**2)+(u_design.shape[1]+3)*nlog)
                            if smooth_u is None or bic < smooth_u['bic']:
                                smooth_u = {'bic': bic, 'width_fraction': width, 'offset_fraction': shift,
                                            'ingress': ingress, 'depth_ppm': float(param[-1]*1e6),
                                            'poly_baseline': param[:-1].tolist()}
        scores[model] = best
    if not scores['U'] or not scores['V']:
        return {'delta_bic': None, 'model': None}
    # U and V are both fitted for depth, level, width, center and ingress.
    return {'delta_bic': scores['V']['chi_square']-scores['U']['chi_square'], 'model': scores['U'],
            'delta_bic_u_vs_smooth': min(null_bics)-smooth_u['bic'] if smooth_u else None,
            'smooth_u_model': smooth_u, 'bins': len(x), 'x': x.tolist(), 'y': f.tolist(), 'err': e.tolist()}


def review_events(curves, candidate):
    events, windows = [], {}
    for curve in curves:
        indexes = np.flatnonzero(np.diff(curve.time) >= GAP_DAYS)
        gap_edges = np.r_[curve.time[indexes], curve.time[indexes+1]]
        dumps = curve.time[(curve.quality & DESAT_FLAG) != 0]
        good = quality_mask(curve)
        for cycle, center in centers(curve, candidate, extent=duration_days(candidate)/2):
            event, window = measure_event(curve, candidate, center, cycle, gap_edges, dumps, good)
            if event is not None:
                events.append(event)
                if window is not None:
                    windows[(event['sector'], cycle)] = window
    measured = [e for e in events if e['snr'] is not None]
    flagged = [e for e in measured if e['sector_edge'] or e['downlink_edge'] or e['momentum_dump'] or e['background_spike']]
    good = [e for e in measured if e not in flagged and e['background_measurable']]
    triggered = [e for e in measured if e['snr'] >= 2.5 and e['depth_ppm'] >= .2*candidate['depth_ppm']]
    tags = []
    if any(e['background_spike'] for e in triggered):
        tags.append('BKG_SPIKE')
    if any(e['sector_edge'] or e['downlink_edge'] or e['momentum_dump'] for e in triggered):
        tags.append('EDGE_ARTIFACT')
    contaminated_signal = sum(e['snr']**2 for e in flagged if e in triggered)
    signal = sum(e['snr']**2 for e in triggered)
    fraction = contaminated_signal/signal if signal else 0.
    score = combined(good)
    sectors = {str(curve.sector): combined([e for e in good if e['sector'] == curve.sector]) for curve in curves}
    repeated = sum(value['snr'] >= 3 for value in sectors.values())
    if tags and (fraction >= .5 or score['snr'] < 7):
        grade = 'REJECT'
    elif score['snr'] < 7 or not good:
        grade = 'INCONCLUSIVE'
    else:
        grade = 'PASS'
    return {'state': grade, 'tags': tags, 'flagged_event_count': len(flagged), 'clean_event_count': len(good),
            'recovered_edge_events': sum(1 for e in events if e.get('edge_recovered')),
            'contaminated_snr2_fraction': fraction, 'clean': score, 'clean_sectors': sectors,
            'repeat_sectors': repeated, 'events': events, 'shape': shape_fit(good, windows, duration_days(candidate))}, windows


def harmonic2_check(curves, candidate, original, scale):
    """SHERLOCK-style out-of-transit two-harmonic (f + 2f) rotational pre-whitening check."""
    p = candidate.get('period_days')
    if not p:
        return None
    d = duration_days(candidate)
    w = 2*np.pi/(p*scale)
    corrected = []
    for curve in curves:
        good = quality_mask(curve) & (abs(phase(curve.time, p, candidate['epoch_btjd'])) > .7*d)
        if good.sum() < 100:
            continue
        t_oot, y_oot = curve.time[good], curve.flux[good]
        t0 = t_oot[0]

        def design(times):
            return np.column_stack((np.ones_like(times), times-t0, np.cos(w*times), np.sin(w*times),
                                    np.cos(2*w*times), np.sin(2*w*times)))

        coef = np.linalg.lstsq(design(t_oot), y_oot, rcond=None)[0]
        model = design(curve.time) @ coef
        corrected.append(replace(curve, flux=curve.flux-model+np.median(model)))
    if len(corrected) < 2:
        return None
    res, _ = review_events(corrected, candidate)
    original_depth = original['clean']['depth_ppm'] or candidate['depth_ppm']
    return {'residual_snr': res['clean']['snr'],
            'retained_depth_fraction': max(0., (res['clean']['depth_ppm'] or 0.)/max(original_depth, 1e-9)),
            'repeat_sectors': res['repeat_sectors'],
            'u_vs_smooth_delta_bic': res['shape'].get('delta_bic_u_vs_smooth')}


def keplerian_duration_ratio(candidate, radius_rsun=None):
    """Compare event duration to the circular Keplerian equatorial transit limit."""
    p = candidate.get('period_days')
    radius = radius_rsun or ((candidate.get('vet') or {}).get('stellar_radius_ruwe') or {}).get('radius_rsun')
    if not p or not radius:
        return None
    mass = max(.15, radius**.8)
    k = math.sqrt(max(candidate.get('depth_ppm') or 0, 0)*1e-6)
    a = 4.2083*p**(2/3)*mass**(1/3)
    t_circ = p/math.pi*math.asin(min(1., radius*(1+k)/a))
    return duration_days(candidate)/t_circ if t_circ > 0 else None


def rotation(curves, candidate, original):
    groups = []
    p = candidate['period_days']
    d = duration_days(candidate)
    for curve in curves:
        distance = abs(phase(curve.time, p, candidate['epoch_btjd'])) if p else abs(curve.time-candidate['epoch_btjd'])
        good = quality_mask(curve) & (distance > .7*d)
        if good.sum() < 100:
            continue
        t, y = curve.time[good], curve.flux[good]
        middle = np.median(y)
        use = abs(y-middle) <= 6*max(robust_sigma(y), 1e-7)
        if use.sum() < 100:
            continue
        trend = np.polyfit(t[use]-t[use][0], y[use], 1)
        t, values, n = bin_values(t[use], y[use]-np.polyval(trend, t[use]-t[use][0]), 30/1440)
        groups.append((str(curve.sector), t[n >= 4], values[n >= 4]))
    if not groups:
        return {'state': 'INCONCLUSIVE', 'reason': 'No sufficient off-transit photometry'}, curves
    groups.append(('joint', np.concatenate([g[1] for g in groups]), np.concatenate([g[2] for g in groups])))
    scans = []
    per_sector = {}
    for sector, t, y in groups:
        if len(t) < 100 or np.std(y) <= 0 or np.ptp(t) < 3:
            continue
        ls = LombScargle(t, y)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            f, power = ls.autopower(minimum_frequency=1/max(100, 3*(p or 1)), maximum_frequency=5.,
                                    samples_per_peak=4, method='fast')
        finite = np.isfinite(power)
        if not finite.any():
            continue
        # Look for a strong global peak and a resolved matching peak near P and the first harmonics.
        peak = int(np.nanargmax(power))
        nearest = []
        for scale in (1., .5, 2.) if p else ():
            target = 1/(p*scale)
            vicinity = finite & (abs(f-target) <= min(.05*target, max(.02*target, .75/np.ptp(t))))
            if np.any(vicinity) and np.ptp(t)/(p*scale) >= 2:
                indexes = np.flatnonzero(vicinity)
                i = indexes[np.argmax(power[indexes])]
                nearest.append((float(power[i]), int(i), scale))
        entry = {'sector': sector, 'peak_period_days': float(1/f[peak]), 'peak_power': float(power[peak]), 'match': None}
        if nearest:
            match_power, i, scale = max(nearest)
            coeff = ls.model_parameters(f[i])
            modulation = float(2*np.hypot(coeff[-2], coeff[-1])*1e6)
            fap = float(np.clip(ls.false_alarm_probability(min(match_power, .999999), method='baluev',
                                                           minimum_frequency=f[0], maximum_frequency=f[-1]), 0, 1))
            strong = bool(match_power >= .15 and match_power >= .75*power[peak] and fap <= 1e-4
                          and modulation >= .8*candidate['depth_ppm'])
            entry['match'] = {'period_days': float(1/f[i]), 'scale': scale, 'power': match_power,
                              'amplitude_ppm_peak_to_peak': modulation, 'fap': fap if np.isfinite(fap) else None,
                              'strong': strong}
            per_sector[sector] = (ls, {factor: float(f[j]) for _, j, factor in nearest})
        scans.append(entry)
    strong = [row for row in scans if row['match'] and row['match']['strong']]
    decisive = any(row['sector'] == 'joint' for row in strong) or len(strong) >= 2 or any(
        row['match']['power'] >= .4 and row['match']['amplitude_ppm_peak_to_peak'] >= candidate['depth_ppm']
        for row in strong)
    quantified = any(row['match'] for row in scans)
    if not decisive:
        return {'state': 'PASS' if quantified and not strong else 'INCONCLUSIVE', 'scans': scans}, curves
    reference = next((row for row in strong if row['sector'] == 'joint'),
                     max(strong, key=lambda row: row['match']['power']))
    scale = reference['match']['scale']
    corrected = []
    corrected_sectors = []
    for curve in curves:
        model = per_sector.get(str(curve.sector))
        if model and scale in model[1]:
            ls, frequencies = model
            sinusoid = ls.model(curve.time, frequencies[scale])
            corrected.append(replace(curve, flux=curve.flux-sinusoid+np.median(sinusoid)))
            corrected_sectors.append(curve.sector)
    residual, _ = review_events(corrected, candidate)
    remaining = residual['clean']['depth_ppm'] or 0.
    original_depth = original['clean']['depth_ppm'] or candidate['depth_ppm']
    fraction = max(0., remaining/max(original_depth, 1e-9))
    if len(corrected) < 2:
        state = 'INCONCLUSIVE'
    elif residual['clean']['snr'] < 7 or fraction < .35:
        state = 'STELLAR_VAR'
    elif residual['state'] == 'PASS' and residual['repeat_sectors'] >= 2:
        state = 'PASS'
    else:
        state = 'INCONCLUSIVE'
    harmonic2 = harmonic2_check(curves, candidate, original, scale)
    return {'state': state, 'scans': scans, 'modeled_sectors': corrected_sectors,
            'retained_depth_fraction': fraction, 'residual': residual, 'harmonic2': harmonic2,
            'tag': 'VAR_WITH_TRANSIT_LIKE_RESIDUAL' if state == 'PASS' else 'OOT_MODULATION_MODELED'}, corrected


def reference_shapes(archive):
    manifest = json.loads((archive / 'tess-golden-inputs.json').read_text(encoding='utf-8'))
    references = [('pi Men c', 261136679, 6.267823, 1325.5041, 2.969, 300., 1.10),
                  ('TOI-700 c', 150428135, 16.0511039, 1821.6219, 1.399, 2800., 0.42)]
    results = []
    for name, tic, period, epoch, hours, depth, radius in references:
        data = [load_curve(archive / item['filename']) for item in manifest if item['tic'] == tic]
        candidate = {'kind': 'periodic', 'period_days': period, 'epoch_btjd': epoch,
                     'duration_days': hours/24, 'depth_ppm': depth}
        clean, _ = review_events(data, candidate)
        delta = clean['shape'].get('delta_bic_u_vs_smooth')
        results.append({'control': name, 'u_vs_smooth_delta_bic': delta, 'clean_snr': clean['clean']['snr'],
                        'repeat_sectors': clean['repeat_sectors'],
                        'duration_to_keplerian_ratio': keplerian_duration_ratio(candidate, radius)})
    return results


if __name__ == '__main__':
    from pathlib import Path
    import sys

    for result in reference_shapes(Path(sys.argv[1])):
        print(json.dumps(result, ensure_ascii=False), flush=True)
