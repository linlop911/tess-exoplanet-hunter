"""Public catalog snapshots and signal-level harmonic matching for the B2–B6 review."""

from collections import defaultdict
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import sys

import requests

from rolling_sources import EB_URL, GAIA
from tess_sources import TAP, atomic_json, session


CTOI = 'https://exofop.ipac.caltech.edu/tess/json/ctois.csv'
OUTPUT = 'deep_vet_b2_b6'
GAIA_FIELDS = {
    'gaia_variables': ('vari_classifier_result', 'source_id,best_class_name,best_class_score'),
    'gaia_eb': ('vari_eclipsing_binary', 'source_id,frequency,frequency_error'),
    'gaia_nss': ('nss_two_body_orbit', 'source_id,nss_solution_type,period,period_error'),
}
HARMONICS = {'P': 1., 'P/3': 1/3, 'P/2': .5, '2P': 2., '3P': 3.}


def number(value):
    try:
        v = float(value)
        if math.isfinite(v):
            return v
    except (TypeError, ValueError):
        pass
    return None


def read_sample(root):
    master = json.loads((root / 'out/MASTER_HITS.json').read_text(encoding='utf-8'))
    names = {f'v21_batch_{i:05d}' for i in range(2, 7)}
    selected = [entry for entry in master if entry.get('batch') in names]
    if len(selected) != 42:
        raise ValueError(f'Expected 42 high-priority signals in batches 2–6, found {len(selected)}')
    return selected


def records(text, fields):
    rows = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not set(fields) <= set(rows.fieldnames or []):
        raise ValueError(f'Unexpected CSV header: {(rows.fieldnames or [])[:10]}')
    return list(rows)


def gaia_query(client, query, fields):
    errors = []
    for url in GAIA:
        try:
            response = client.post(url, data={'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'csv', 'QUERY': query},
                                   timeout=(8, 36))
            response.raise_for_status()
            return response.text, records(response.text, fields), url
        except (requests.RequestException, ValueError) as exc:
            errors.append(f'{url}: {exc}')
    raise RuntimeError(' | '.join(errors))


def capture(client, cache, name, url, fields, query=None, gaia=False):
    csv_path = cache / f'{name}.csv'
    meta_path = cache / f'{name}.json'
    if csv_path.is_file() and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        return records(csv_path.read_text(encoding='utf-8'), fields), meta
    if gaia:
        payload, rows, url = gaia_query(client, query, fields)
    else:
        params = {'query': query, 'format': 'csv'} if query else None
        response = client.get(url, params=params, timeout=(8, 45))
        response.raise_for_status()
        payload = response.text
        rows = records(payload, fields)
    temp = csv_path.with_suffix('.part')
    temp.write_text(payload, encoding='utf-8')
    temp.replace(csv_path)
    meta = {'source': url, 'query': query, 'fetched_utc': datetime.now(timezone.utc).isoformat(), 'rows': len(rows)}
    atomic_json(meta_path, meta)
    return rows, meta


def source_ids(sample):
    return {entry['tic']: str(entry['vet']['stellar_radius_ruwe'].get('gaia_dr3') or '') for entry in sample}


def load_catalogs(root, sample):
    cache = root / 'out' / OUTPUT / 'catalog_sources'
    cache.mkdir(parents=True, exist_ok=True)
    ids = source_ids(sample)
    gaia_ids = ','.join(sorted({value for value in ids.values() if value.isdecimal()}))
    tids = ','.join(map(str, sorted(ids)))
    formatted = ','.join(f"'TIC {tic}'" for tic in sorted(ids))
    ps = f'select tic_id,pl_name,pl_orbper,pl_orbpererr1,pl_tranmid,pl_tranmiderr1 from ps where default_flag=1 and tic_id in ({formatted})'
    toi = f'select tid,toi,tfopwg_disp,pl_orbper,pl_orbpererr1,pl_tranmid,pl_tranmiderr1 from toi where tid in ({tids})'
    specs = [('ps', TAP, ['tic_id', 'pl_orbper'], ps, False), ('toi', TAP, ['tid', 'pl_orbper'], toi, False),
             ('ctoi', CTOI, ['TIC ID', 'Period (days)', 'CTOI Lastmod'], None, False),
             ('tess_eb', EB_URL, ['tess_id', 'period', 'bjd0'], None, False)]
    for name, (table, cols) in GAIA_FIELDS.items():
        if gaia_ids:
            specs.append((name, None, cols.split(','), f'select {cols} from gaiadr3.{table} where source_id in ({gaia_ids})', True))
    snapshots, audit = {}, {}
    with session() as client:
        for name, url, required, query, gaia in specs:
            try:
                snapshots[name], audit[name] = capture(client, cache, name, url, required, query, gaia)
                print(f'catalog {name}: {audit[name]["rows"]} rows', flush=True)
            except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
                audit[name] = {'source': url, 'query': query, 'error': str(exc)[:700]}
                print(f'catalog {name} unavailable: {exc}', flush=True)
    atomic_json(cache.parent / 'catalog_audit.json', audit)
    by_tic = defaultdict(list)
    gaia_tics = defaultdict(list)
    for tic, gid in ids.items():
        if gid:
            gaia_tics[gid].append(tic)
    for name, rows in snapshots.items():
        for row in rows:
            if name.startswith('gaia_'):
                matched = gaia_tics[str(row['source_id'])]
            else:
                field = {'ps': 'tic_id', 'toi': 'tid', 'ctoi': 'TIC ID', 'tess_eb': 'tess_id'}[name]
                identity = row[field].strip().removeprefix('TIC ')
                matched = [int(identity)] if identity.isdecimal() and int(identity) in ids else []
            for tic in matched:
                by_tic[tic].append((name, row))
    return by_tic, audit


def match_harmonic(detected, reference):
    if not detected or not reference or reference <= 0:
        return None
    name, scale = min(HARMONICS.items(), key=lambda item: abs(detected/(reference*item[1])-1))
    error = abs(detected/(reference*scale)-1)
    return (name, error) if error <= .005 else None


def evidence(name, row):
    if name in ('ps', 'toi'):
        field = 'pl_name' if name == 'ps' else 'toi'
        return {'id': row[field], 'period': number(row['pl_orbper']), 'epoch': number(row['pl_tranmid']),
                'period_error': number(row.get('pl_orbpererr1')), 'epoch_error': number(row.get('pl_tranmiderr1')),
                'disposition': row.get('tfopwg_disp') or 'CONFIRMED'}
    if name == 'ctoi':
        return {'id': row.get('CTOI'), 'period': number(row['Period (days)']),
                'epoch': number(row.get('Transit Epoch (BJD)')), 'period_error': number(row.get('Period (days) Error')),
                'epoch_error': number(row.get('Transit Epoch (BJD) err')),
                'disposition': row.get('TFOPWG Disposition') or row.get('User Disposition'), 'lastmod': row['CTOI Lastmod']}
    if name == 'tess_eb':
        return {'id': row.get('signal_id'), 'period': number(row['period']), 'epoch': number(row['bjd0']),
                'period_error': number(row.get('period_uncert')), 'epoch_error': number(row.get('bjd0_uncert')),
                'lastmod': row.get('date_modified')}
    if name == 'gaia_eb':
        frequency = number(row['frequency'])
        frequency_error = number(row['frequency_error'])
        return {'id': row['source_id'], 'period': 1/frequency if frequency and frequency > 0 else None,
                'period_error': frequency_error/frequency**2 if frequency and frequency_error else None}
    if name == 'gaia_nss':
        return {'id': row['source_id'], 'period': number(row['period']), 'period_error': number(row['period_error']),
                'disposition': row['nss_solution_type']}
    return {'id': row['source_id'], 'period': None, 'disposition': row['best_class_name'],
            'class_score': number(row['best_class_score'])}


def compare(candidate, rows, audit):
    review = {'state': 'UNCATALOGED', 'matches': [], 'host_associations': [], 'missing_sources': []}
    priority = {'ps': 'CONFIRMED', 'toi': 'KNOWN_TOI', 'ctoi': 'KNOWN_CTOI', 'tess_eb': 'KNOWN_EB',
                'gaia_eb': 'GAIA_EB', 'gaia_nss': 'GAIA_SB_OR_NSS'}
    period = candidate['period_days']
    for source, row in rows:
        item = {'source': source, **evidence(source, row)}
        match = match_harmonic(period, item['period'])
        if not match:
            review['host_associations'].append(item)
            continue
        item.update(relation=match[0], relative_period_error=match[1])
        if item.get('epoch') is not None:
            reference = item['epoch'] - 2457000 if item['epoch'] > 2457000 else item['epoch']
            cycle = (candidate['epoch_btjd'] - reference)/item['period']
            item['phase_offset_days'] = abs(cycle-round(cycle))*item['period']
        review['matches'].append(item)
    review['missing_sources'] = [key for key in ('ps', 'toi', 'ctoi', 'tess_eb', *GAIA_FIELDS)
                                 if key not in audit or 'error' in audit[key]]
    if not source_ids([candidate])[candidate['tic']]:
        review['missing_sources'].append('gaia_dr3_crossmatch')
    if review['matches']:
        review['state'] = priority[review['matches'][0]['source']]
    elif review['missing_sources']:
        review['state'] = 'CATALOG_INCONCLUSIVE'
    return review


def neighbors(client, cache, tic, ra, dec):
    fields = 'source_id,ra,dec,pmra,pmdec,ref_epoch,phot_g_mean_mag'
    query = (f"select top 250 {fields} from gaiadr3.gaia_source where phot_g_mean_mag <= 18 "
             f"and 1=contains(point('ICRS',ra,dec),circle('ICRS',{ra:.9f},{dec:.9f},0.085)) order by phot_g_mean_mag")
    rows, _ = capture(client, cache, f'gaia_neighbors_{tic}', None, fields.split(','), query, True)
    return rows


if __name__ == '__main__':
    review_root = Path(sys.argv[1])
    load_catalogs(review_root, read_sample(review_root))
