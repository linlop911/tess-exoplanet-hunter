"""Difference-image centroids referred to the target TIC WCS, with Gaia neighbors."""

import json
import math
import warnings

from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
import numpy as np
from scipy.optimize import least_squares

from deep_vet_catalog import neighbors, number
from rolling_sources import GIB, Ledger
from tess_sources import atomic_json
from tess_vetting import duration_days, robust_sigma


class DeepBudget(Ledger):
    def charge(self, count):
        from rolling_sources import TransferLimit

        with self.lock:
            if self.used + count > GIB:
                raise TransferLimit('Deep-review 1 GiB TPF transfer cap would be exceeded')
            self.used += count
            atomic_json(self.path, {'bytes': self.used, 'limit': GIB})


def mean_images(table, events, duration, period, aperture):
    t = np.asarray(table['TIME'], dtype=float)
    quality = np.isfinite(t) & (table['QUALITY'] == 0)
    flux = table['FLUX']
    values = []
    dt = min(duration*1.45, period*.47) if period else duration*1.45
    for event in events:
        time = t-event['btjd']
        inside = quality & (abs(time) <= .42*duration)
        left = quality & (time < -.68*duration) & (time >= -dt)
        right = quality & (time > .68*duration) & (time <= dt)
        if min(inside.sum(), left.sum(), right.sum()) < 8:
            continue
        a, b, c = (np.asarray(flux[mask], dtype=float) for mask in (inside, left, right))
        # Same-event shoulders are averaged equally to cancel a first-order drift.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            diff = (np.nanmean(b, axis=0)+np.nanmean(c, axis=0))/2 - np.nanmean(a, axis=0)
            var = np.nanvar(a, axis=0, ddof=1)/len(a) + np.nanvar(b, axis=0, ddof=1)/(4*len(b))
            var += np.nanvar(c, axis=0, ddof=1)/(4*len(c))
        # Per-pixel errors are floored to avoid a constant pixel dominating the stack.
        measured = var[aperture & np.isfinite(var) & (var > 0)]
        if len(measured) < 5:
            continue
        var = np.maximum(var, np.percentile(measured, 20)*.2)*4
        weight = np.divide(1., var, out=np.zeros_like(var), where=aperture & np.isfinite(diff) & np.isfinite(var) & (var > 0))
        values.append((np.nan_to_num(diff), weight))
    if not values:
        return None
    weights = sum(value[1] for value in values)
    image = np.divide(sum(value[0]*value[1] for value in values), weights, out=np.zeros_like(weights), where=weights > 0)
    error = np.sqrt(np.divide(1., weights, out=np.full_like(weights, np.inf), where=weights > 0))
    return image, error, len(values)


def centroid(image, error):
    yy, xx = np.indices(image.shape)
    good = np.isfinite(error) & (error > 0)
    snr_map = np.divide(image, error, out=np.zeros_like(image), where=good)
    index = np.unravel_index(np.argmax(snr_map), snr_map.shape)
    cy, cx = map(float, index)
    box = good & (abs(xx-cx) <= 3) & (abs(yy-cy) <= 3)
    if snr_map[index] < 3 or box.sum() < 12:
        return None
    x, y, value, sigma = xx[box], yy[box], image[box], error[box]

    def residual(a):
        amp, x0, y0, width, background = a
        model = background + amp*np.exp(-((x-x0)**2+(y-y0)**2)/(2*width**2))
        return (model-value)/sigma

    result = least_squares(residual, [float(image[index]), cx, cy, .8, 0.],
                           bounds=([0., cx-1.5, cy-1.5, .35, -np.inf], [np.inf, cx+1.5, cy+1.5, 2., np.inf]),
                           max_nfev=150)
    amp, x0, y0, width, _ = result.x
    if not result.success or np.linalg.matrix_rank(result.jac) < 5:
        return None
    cov = np.linalg.inv(result.jac.T@result.jac)*max(1., np.sum(result.fun**2)/(len(value)-5))
    fit_snr = amp/np.sqrt(cov[0, 0]) if cov[0, 0] > 0 else 0.
    position_sigma = float(np.sqrt(max(cov[1, 1], cov[2, 2], .01)))
    if not np.all(np.isfinite((fit_snr, x0, y0, position_sigma))):
        return None
    return {'x': float(x0), 'y': float(y0), 'sigma_px': position_sigma, 'snr': float(fit_snr),
            'width_px': float(width), 'peak_pixel_snr': float(snr_map[index])}


def project_neighbors(world, rows, epoch, shape):
    stars = []
    for row in rows:
        ra, dec = float(row['ra']), float(row['dec'])
        years = (epoch - (number(row['ref_epoch']) or 2016.0))
        cos_dec = math.cos(math.radians(dec))
        if abs(cos_dec) > .001:
            ra += years*(number(row['pmra']) or 0.)/(3.6e6*cos_dec)
            dec += years*(number(row['pmdec']) or 0.)/3.6e6
        x, y = map(float, world.world_to_pixel_values(ra, dec))
        if -1 <= x <= shape[1] and -1 <= y <= shape[0]:
            stars.append({'source_id': row['source_id'], 'x': x, 'y': y, 'g_mag': float(row['phot_g_mean_mag'])})
    return stars


def check_pixels(client, cache, tpf, candidate, sector_events):
    with fits.open(tpf, memmap=True) as file:
        primary = file[0].header
        hdu = file[1]
        if hdu.header.get('BJDREFI') != 2457000 or hdu.header.get('TIMESYS') != 'TDB':
            raise ValueError(f'{tpf.name}: TPF does not use BTJD/TDB')
        flags = np.asarray(file['APERTURE'].data, dtype=int)
        measured = mean_images(hdu.data, sector_events, duration_days(candidate), candidate['period_days'], (flags & 1) != 0)
        if not measured:
            return {'state': 'INCONCLUSIVE', 'reason': '无足够的同次凌星内外像素采样'}
        image, error, count = measured
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', FITSFixedWarning)
            world = WCS(file['APERTURE'].header).celestial
        if not world.has_celestial:
            raise ValueError(f'{tpf.name}: no celestial aperture WCS')
        ra, dec = float(primary['RA_OBJ']), float(primary['DEC_OBJ'])
        target_x, target_y = map(float, world.world_to_pixel_values(ra, dec))
        epoch = 2000. + (np.mean([entry['btjd'] for entry in sector_events]) + 2457000 - 2451545)/365.25
        rows = neighbors(client, cache, candidate['tic'], ra, dec)
        gaia = project_neighbors(world, rows, epoch, flags.shape)
        gaia_id = str(candidate['vet']['stellar_radius_ruwe']['gaia_dr3'])
        target_gaia = next((row for row in gaia if row['source_id'] == gaia_id), None)
        if target_gaia is None:
            return {'state': 'INCONCLUSIVE', 'reason': 'Gaia 目标交叉标识未落入有效像素图'}
        if math.hypot(target_x-target_gaia['x'], target_y-target_gaia['y']) > .75:
            return {'state': 'INCONCLUSIVE', 'reason': 'TIC 与 Gaia 目标坐标相差超过 0.75 像素'}
        fit = centroid(image, error)
        if fit is None:
            return {'state': 'INCONCLUSIVE', 'reason': '差异图无可拟合的显著亮点'}
        offset = math.hypot(fit['x'] - target_x, fit['y'] - target_y)
        alternative = []
        for star in gaia:
            if star['source_id'] != gaia_id and star['g_mag'] <= target_gaia['g_mag'] + 4:
                separation = math.hypot(fit['x']-star['x'], fit['y']-star['y'])
                if separation <= .7 and offset-separation >= .35:
                    alternative.append({**star, 'distance_px': separation})
        enough = fit['snr'] >= 5 and fit['sigma_px'] <= .5
        state = 'INCONCLUSIVE' if not enough else 'BG_CONTAMINATION' if offset > 1 or alternative else 'CENTERED'
        return {'state': state, 'offset_px': offset, 'target': [target_x, target_y], 'centroid': fit, 'events': count,
                'gaia_neighbors': gaia, 'neighbor_match': sorted(alternative, key=lambda item: item['distance_px']),
                'image': image.tolist(), 'sector': primary['SECTOR'], 'filename': tpf.name}
