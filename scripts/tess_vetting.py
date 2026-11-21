"""Transit-shape, event, catalog and pixel evidence for SPOC candidates."""

from collections import defaultdict
import csv
import io

from astropy.io import fits
import numpy as np

from golden_calibration import CADENCE, phase
from tess_sources import TAP


def robust_sigma(x):
    return 1.4826 * np.median(np.abs(x - np.median(x)))


def event_depth(t, y, epoch, duration):
    left, right = np.searchsorted(t, [epoch - duration * 1.5, epoch + duration * 1.5])
    x, f = (t[left:right] - epoch) / duration, y[left:right]
    inside = np.abs(x) <= .5
    side = ~inside
    expected = duration / CADENCE
    if (np.count_nonzero(inside) < .6 * expected or np.count_nonzero(x < -.5) < .5 * expected
            or np.count_nonzero(x > .5) < .5 * expected):
        return None
    noise = robust_sigma(f[side])
    if not np.isfinite(noise) or noise <= 0:
        return None
    depth = np.median(f[side]) - np.mean(f[inside])
    # Floor the error using 20-minute bins to limit sensitivity to serially correlated cadences.
    bin_size = 10
    side_flux = f[side]
    count = len(side_flux) // bin_size
    red_noise = robust_sigma(side_flux[:count * bin_size].reshape(count, bin_size).mean(axis=1)) if count >= 4 else 0.
    noise = max(noise, np.sqrt(bin_size) * red_noise)
    error = noise * np.sqrt(1 / inside.sum() + 1 / side.sum())
    return float(depth), float(error), float(noise), float(np.max(f[side]) - np.median(f[side]))


def aggregated(events):
    if len(events) < 2:
        return None
    depth, error = np.asarray(events)[:, :2].T
    mean_error = np.sqrt(np.sum(error**2)) / len(error)
    actual_error = np.std(depth, ddof=1) / np.sqrt(len(error))
    return float(np.mean(depth)), float(max(mean_error, actual_error))


def duration_days(candidate):
    return candidate['duration_days'] if candidate['kind'] == 'periodic' else candidate['duration_hours'] / 24


def _smooth_delta_bic(signed_x, f):
    labels = np.linspace(-1.5, 1.5, 37)
    indices = np.digitize(signed_x, labels) - 1
    grid, values, spread = [], [], []
    for index in range(36):
        bf = f[indices == index]
        if len(bf) >= 4:
            grid.append((labels[index] + labels[index + 1]) / 2)
            values.append(np.median(bf))
            spread.append(robust_sigma(bf) / np.sqrt(max(1., len(bf) / 10)))
    x, y, e = map(np.asarray, (grid, values, spread))
    if len(y) < 18 or not np.any(e > 0):
        return None
    e = np.maximum(e, max(np.median(e[e > 0]) * .5, 1e-9))
    nlog = np.log(len(x))
    continua = [np.vander(x, order + 1, increasing=True) for order in (2, 4)]
    null_bic = min(float(np.sum(((y - d @ np.linalg.lstsq(d / e[:, None], y / e, rcond=None)[0]) / e)**2) + d.shape[1] * nlog)
                   for d in continua)
    best_u = None
    for width in (.65, .85, 1., 1.2):
        for shift in (-.1, 0., .1):
            for ingress in (.12, .20, .27):
                template = np.clip((.5 * width - abs(x - shift)) / (width * ingress), 0, 1)
                for cont in continua:
                    u_design = np.column_stack((cont, -template))
                    param = np.linalg.lstsq(u_design / e[:, None], y / e, rcond=None)[0]
                    if param[-1] <= 0:
                        continue
                    bic = float(np.sum(((y - u_design @ param) / e)**2) + (u_design.shape[1] + 3) * nlog)
                    if best_u is None or bic < best_u:
                        best_u = bic
    return None if best_u is None else null_bic - best_u


def lightcurve_checks(t, y, candidate):
    epoch = candidate['epoch_btjd']
    duration = duration_days(candidate)
    p = candidate['period_days']
    if p is None:
        offset = t - epoch
        odd_even = secondary = {'state': 'NOT_APPLICABLE'}
        measurement = event_depth(t, y, epoch, duration)
        flares = {'state': 'INCONCLUSIVE'} if measurement is None else {
            'state': 'FLAG' if measurement[3] > 5 * measurement[2] else 'PASS',
            'depth_ppm': measurement[0] * 1e6,
        }
    else:
        offset = phase(t, p, epoch)
        first = int(np.ceil((t[0] - epoch) / p))
        last = int(np.floor((t[-1] - epoch) / p))
        primary = [[], []]
        secondaries = []
        for cycle in range(first, last + 1):
            event = event_depth(t, y, epoch + cycle * p, duration)
            secondary_event = event_depth(t, y, epoch + (cycle + .5) * p, duration)
            if event is not None:
                primary[cycle % 2].append(event)
            if secondary_event is not None:
                secondaries.append(secondary_event)
        a, b = map(aggregated, primary)
        odd_even = {'state': 'INCONCLUSIVE', 'events': [len(group) for group in primary]}
        if a and b:
            significance = abs(a[0] - b[0]) / np.hypot(a[1], b[1])
            odd_even.update(state='FLAG' if significance > 3 else 'PASS', significance=float(significance))
        second = aggregated(secondaries)
        secondary = {'state': 'INCONCLUSIVE', 'events': len(secondaries)}
        sig_half = None
        if second:
            sig_half = float(second[0] / second[1])
            secondary.update(significance=sig_half)
        # Full-phase secondary scan (phi in [0.1, 0.9], excluding primary transit window)
        # catches eccentric EBs and 3P/4P/5P rotational harmonic aliases.
        phi = ((t - epoch) % p) / p
        excl = max(.08, 1.5 * duration / p)
        oof = (phi > excl) & (phi < 1 - excl)
        sig_off, best_phi = None, None
        if np.count_nonzero(oof) >= 60:
            step = max(.01, min(.05, .5 * duration / p))
            edges = np.arange(excl, 1 - excl + step * .5, step)
            if len(edges) >= 4:
                idx = np.clip(np.digitize(phi[oof], edges) - 1, 0, len(edges) - 2)
                counts = np.bincount(idx, minlength=len(edges) - 1)
                sums = np.bincount(idx, weights=y[oof], minlength=len(edges) - 1)
                valid_bins = counts >= 10
                if np.any(valid_bins):
                    means = np.where(valid_bins, sums / np.maximum(counts, 1), np.inf)
                    picked = []
                    for b in np.argsort(means):
                        if not valid_bins[b] or len(picked) >= 3:
                            break
                        cphi = float((edges[b] + edges[b + 1]) / 2)
                        if abs(cphi - .5) <= .01 or any(abs(cphi - prev) < max(.03, duration / p) for prev in picked):
                            continue
                        picked.append(cphi)
                    for cand_phi in picked:
                        off_events = [ev for cycle in range(first, last + 1)
                                      if (ev := event_depth(t, y, epoch + (cycle + cand_phi) * p, duration)) is not None]
                        off_agg = aggregated(off_events)
                        if off_agg:
                            s_val = float(off_agg[0] / off_agg[1])
                            if sig_off is None or s_val > sig_off:
                                sig_off, best_phi = s_val, cand_phi
                                secondary.update(max_off_phase=best_phi, max_off_phase_significance=sig_off,
                                                 off_phase_events=len(off_events))
        if (sig_half is not None and sig_half > 4) or (sig_off is not None and sig_off > 5):
            secondary['state'] = 'FLAG'
        elif sig_half is not None or sig_off is not None:
            secondary['state'] = 'PASS'
        flares = {'state': 'NOT_APPLICABLE'}
    near = np.abs(offset) < 1.5 * duration
    x = np.abs(offset[near]) / duration
    f = y[near]
    morphology = {'state': 'INCONCLUSIVE'}
    if len(f) > 30 and np.count_nonzero(x < .2) > 5:
        band = x > .65
        noise = robust_sigma(f[band]) if np.count_nonzero(band) > 10 else 0
        if noise > 0:
            scores = []
            for ingress in (.15, .40):
                template = np.clip((.55 - x) / ingress, 0, 1)
                matrix = np.stack((np.ones_like(x), -template), axis=1)
                fit = np.linalg.lstsq(matrix, f, rcond=None)[0]
                scores.append(float(np.sum(((f - matrix @ fit) / noise)**2)))
            delta = scores[1] - scores[0]
            smooth_bic = _smooth_delta_bic(offset[near] / duration, f)
            state = 'FLAG' if delta < -12 else 'PASS' if (delta > 12 and (smooth_bic is None or smooth_bic >= 6)) else 'INCONCLUSIVE'
            morphology = {'state': state, 'delta_chi_square_v_minus_u': delta, 'delta_bic_u_vs_smooth': smooth_bic}
    return {'odd_even': odd_even, 'secondary': secondary, 'shape': morphology, 'flare_window': flares}


def catalogs(client, ids):
    ids = sorted(set(ids))
    rows = defaultdict(list)
    fields = 'pl_orbper,pl_orbpererr1,pl_tranmid,pl_tranmiderr1'
    ps_ids = ','.join(f"'TIC {tic}'" for tic in ids)
    toi_ids = ','.join(map(str, ids))
    queries = {
        'ps': (f'select tic_id,pl_name,pl_trandep,{fields} from ps '
               f'where default_flag=1 and tic_id in ({ps_ids})'),
        'toi': f'select tid,toi,tfopwg_disp,{fields} from toi where tid in ({toi_ids})',
    }
    for name, query in queries.items():
        response = client.get(TAP, params={'query': query, 'format': 'csv'}, timeout=(8, 60))
        response.raise_for_status()
        records = csv.DictReader(io.StringIO(response.text))
        field = 'tic_id' if name == 'ps' else 'tid'
        if field not in (records.fieldnames or ()) or 'pl_orbper' not in records.fieldnames:
            raise ValueError(f'TAP {name} returned an unexpected CSV header')
        for row in records:
            tic = int(row[field].removeprefix('TIC '))
            rows[tic].append({**row, 'table': name})
    return rows


def float_field(row, key):
    value = row.get(key)
    return float(value) if value else None


def match_catalog(candidate, rows):
    if rows is None:
        return {'state': 'CATALOG_UNAVAILABLE'}
    matches, uncertain = [], []
    p = candidate['period_days']
    epoch = candidate['epoch_btjd']
    tolerance = duration_days(candidate) / 2
    for row in rows:
        rp, ref = float_field(row, 'pl_orbper'), float_field(row, 'pl_tranmid')
        if rp is not None and rp <= 0:
            continue
        harmonics = [h for h in (1, .5, 2, 1/3, 3) if rp and p and abs(p / (rp * h) - 1) <= .005]
        if p and (rp is None or not harmonics):
            continue
        label = row.get('pl_name') or row.get('toi')
        item = {'name': label, 'table': row['table'], 'disposition': row.get('tfopwg_disp')}
        if ref is None or rp is None:
            uncertain.append(item)
            continue
        ref -= 2457000
        cycle = round((epoch - ref) / rp)
        sigma = np.hypot(float_field(row, 'pl_tranmiderr1') or 0,
                         cycle * (float_field(row, 'pl_orbpererr1') or 0))
        distance = abs(epoch - (ref + cycle * rp))
        if distance <= tolerance + 3 * sigma:
            if 3 * sigma > tolerance:
                uncertain.append(item)
            else:
                matches.append({**item, 'relation': 'EPOCH_ONLY' if p is None else
                                'SAME_PERIOD' if harmonics[0] == 1 else 'HARMONIC', 'timing_days': distance})
    if matches:
        state = 'CONFIRMED' if any(item['table'] == 'ps' for item in matches) else 'KNOWN_TOI'
    else:
        state = 'CATALOG_INCONCLUSIVE' if uncertain else 'UNCATALOGED'
    return {'state': state, 'cataloged_host': bool(rows), 'matches': matches, 'uncertain': uncertain}


def pixel_check(path, candidate):
    p, epoch = candidate['period_days'], candidate['epoch_btjd']
    dur = duration_days(candidate)
    with fits.open(path, memmap=True) as hdus:
        table = hdus[1].data
        flags = hdus['APERTURE'].data.astype(int)
        t = np.asarray(table['TIME'], dtype=float)
        valid = np.isfinite(t) & (table['QUALITY'] == 0)
        x = phase(t, p, epoch) if p else t - epoch
        inside = valid & (abs(x) < dur / 2)
        outside = valid & (abs(x) > dur * .7) & (abs(x) < dur * 1.5)
        if min(inside.sum(), outside.sum()) < 20:
            return {'state': 'INCONCLUSIVE', 'reason': 'Insufficient in/out pixel cadences'}
        flux = np.asarray(table['FLUX'], dtype=float)
        reference = np.nanmedian(flux[outside], axis=0)
        difference = reference - np.nanmedian(flux[inside], axis=0)
        error = np.nanstd(flux[outside], axis=0) * np.sqrt(1 / inside.sum() + 1 / outside.sum())
        mask = ((flags & 1) != 0) & np.isfinite(reference) & np.isfinite(error) & (error > 0)
        aperture = (flags & 2) != 0
        significant = mask & (difference > 3 * error)
        if not np.any(significant) or not np.any(aperture & mask):
            return {'state': 'INCONCLUSIVE', 'reason': 'No significant difference image'}
        yy, xx = np.mgrid[:flags.shape[0], :flags.shape[1]]
        weights = np.where(significant, difference, 0.)
        center_weights = np.where(aperture & mask, np.maximum(reference, 0), 0.)
        if center_weights.sum() <= 0:
            return {'state': 'INCONCLUSIVE', 'reason': 'Invalid aperture flux'}
        displacement = np.hypot(*[np.sum(axis * weights) / weights.sum() -
                                  np.sum(axis * center_weights) / center_weights.sum() for axis in (xx, yy)])
        peak = np.unravel_index(np.argmax(center_weights), flags.shape)
        core = ((xx - peak[1])**2 + (yy - peak[0])**2 <= 1) & aperture & mask
        whole = aperture & mask
        depth_core = np.sum(difference[core]) / np.sum(reference[core])
        depth_whole = np.sum(difference[whole]) / np.sum(reference[whole])
        fraction = weights[~aperture].sum() / weights.sum()
        flagged = displacement >= 1 and fraction >= .25
        return {'state': 'FLAG' if flagged else 'PASS', 'centroid_offset_px': float(displacement),
                'core_depth_ppm': float(depth_core * 1e6), 'aperture_depth_ppm': float(depth_whole * 1e6),
                'significant_flux_outside_aperture_fraction': float(fraction), 'difference_image':
                np.where(mask, difference, 0.).tolist()}
