"""Jetson-native selection, public catalogs and verified resumable FITS transfers."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import threading
import time

from astropy.io import fits
from astropy.io.fits.verify import VerifyError
import requests

from tess_campaign import control_candidates, read_json, sector_products
from tess_sources import TAP, atomic_json, digest, mast, products_for_sector, session


GIB = 1024**3
LIMIT = 3 * GIB
EB_URL = ('https://archive.stsci.edu/hlsps/tess-ebs/'
          'hlsp_tess-ebs_tess_lcf-ffi_s0001-s0026_tess_v1.0_cat.csv')
BULK_PAGE = 'https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_ffi-tp-lc-dv.html'
GAIA = ('https://gaia.aip.de/tap/sync', 'https://gea.esac.esa.int/tap-server/tap/sync')


class NetworkBatch(RuntimeError):
    pass


class TransferLimit(RuntimeError):
    pass


class Ledger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.used = read_json(path)['bytes'] if path.exists() else 0

    def charge(self, count):
        with self.lock:
            if self.used + count > LIMIT:
                raise TransferLimit('3 GiB batch transfer budget exhausted')
            self.used += count
            atomic_json(self.path, {'bytes': self.used, 'limit': LIMIT})


def inspect_fits(path, row, response_md5):
    if path.stat().st_size % 2880:
        raise ValueError('Incomplete FITS block')
    if response_md5:
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'md5').hexdigest() != response_md5:
                raise ValueError('AWS object ETag checksum mismatch')
    published_mismatches = []
    with fits.open(path, memmap=True) as hdus:
        hdus.verify('exception')
        if hdus[0].header['TICID'] != row['tic'] or hdus[0].header['SECTOR'] != row['sector']:
            raise ValueError('Wrong FITS target or sector')
        for index, hdu in enumerate(hdus):
            if 'CHECKSUM' in hdu.header and hdu.verify_checksum() != 1:
                published_mismatches.append(index)
    if published_mismatches and not response_md5:
        raise ValueError('FITS checksum mismatch without verified AWS object digest')
    return published_mismatches


def transfer_one(client, url, row, target, ledger, mirror):
    partial = target.with_suffix(f'.{mirror}.part')
    marker = partial.with_suffix(partial.suffix + '.json')
    with client.head(url, allow_redirects=True, timeout=(8, 30)) as head:
        head.raise_for_status()
        total = int(head.headers['Content-Length'])
        etag = head.headers.get('ETag', '').strip('"')
        last_modified = head.headers.get('Last-Modified')
        if not 0 < total <= 300*1024**2 or total % 2880:
            raise ValueError(f'Invalid remote FITS length {total}')
    identity = {'url': url, 'size': total, 'etag': etag, 'last_modified': last_modified}
    if not marker.is_file() or read_json(marker) != identity or not (etag or last_modified):
        partial.unlink(missing_ok=True)
        atomic_json(marker, identity)
    start = partial.stat().st_size if partial.is_file() else 0
    if start > total:
        partial.unlink()
        start = 0
    if start < total:
        headers = {'Range': f'bytes={start}-'} if start else {}
        if start and etag:
            headers['If-Range'] = f'"{etag}"'
        elif start:
            headers['If-Range'] = last_modified
        with client.get(url, headers=headers, stream=True, timeout=(8, 40)) as response:
            response.raise_for_status()
            if start and response.status_code == 206:
                if response.headers.get('Content-Range') != f'bytes {start}-{total-1}/{total}':
                    raise ValueError('Invalid HTTP resume range')
            else:
                start = 0
            remaining = total - start
            if int(response.headers['Content-Length']) != remaining:
                raise ValueError('Remote length changed during download')
            ledger.charge(remaining)
            received = 0
            clock = time.monotonic()
            with partial.open('ab' if start else 'wb') as stream:
                for block in response.iter_content(256*1024):
                    received += len(block)
                    if received > remaining:
                        raise ValueError('Oversized HTTP response')
                    stream.write(block)
                    delay = received / (6*1024**2) - (time.monotonic()-clock)
                    if delay > 0:
                        time.sleep(delay)
            if received != remaining:
                raise ValueError('Incomplete HTTP response')
    md5 = etag.lower() if mirror == 'aws' and re.fullmatch(r'[0-9a-fA-F]{32}', etag) else None
    try:
        hdu_mismatches = inspect_fits(partial, row, md5)
    except (OSError, ValueError, KeyError, VerifyError):
        partial.unlink(missing_ok=True)
        raise
    checksum = digest(partial)
    partial.replace(target)
    return {**row, 'size': total, 'sha256': checksum, 'transfer_source': mirror, 'resumed_from': start,
            'aws_etag_md5_verified': bool(md5), 'published_fits_checksum_mismatches': hdu_mismatches}


def fetch_fits(client, row, directory, ledger):
    path = directory / row['filename']
    receipt = path.with_suffix('.receipt.json')
    if path.is_file() and receipt.is_file():
        previous = read_json(receipt)
        if digest(path) == previous['sha256']:
            return previous
        path.unlink()
    fallback = f'https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:TESS/product/{path.name}'
    failures = []
    for mirror, url in (('aws', row['url']), ('aws', row['url']), ('mast', fallback)):
        try:
            result = transfer_one(client, url, row, path, ledger, mirror)
            atomic_json(receipt, result)
            return result
        except (requests.RequestException, ValueError, OSError, KeyError, VerifyError) as exc:
            failures.append(f'{mirror}: {exc}')
    raise NetworkBatch(f'{path.name}: ' + ' | '.join(failures))


def latest_sectors(client):
    result = client.get(BULK_PAGE, timeout=(8, 40))
    result.raise_for_status()
    sectors = sorted({int(number) for number in re.findall(r'tesscurl_sector_(\d+)_lc\.sh', result.text)
                      if 0 < int(number) < 1000})
    if len(sectors) < 20:
        raise NetworkBatch('Incomplete MAST public sector index')
    return sectors


def metadata(client, cache, ids):
    previous = read_json(cache) if cache.exists() else {}
    missing = sorted(set(ids) - set(previous), key=int)
    for start in range(0, len(missing), 250):
        chunk = missing[start:start+250]
        response = mast(client, 'Mast.Catalogs.Filtered.Tic', '*', [{'paramName': 'ID', 'values': chunk}],
                        page_size=len(chunk)+5)
        if response['paging']['pagesFiltered'] > 1:
            raise NetworkBatch('TIC catalog returned more than one page for a bounded request')
        previous.update(dict.fromkeys(chunk))
        for row in response['data']:
            previous[str(row['ID'])] = {'tmag': row.get('Tmag'), 'radius': row.get('rad'),
                                       'logg': row.get('logg'), 'distance_pc': row.get('d'),
                                       'lumclass': row.get('lumclass'), 'gaia_dr2': row.get('GAIA'),
                                       'contamination': row.get('contratio')}
        atomic_json(cache, previous)
    return {tic: previous[tic] for tic in ids if previous.get(tic)}


def gaia_metadata(client, rows):
    ids = sorted({int(item['gaia_dr2']) for item in rows.values()
                  if str(item.get('gaia_dr2', '')).isdigit() and int(item['gaia_dr2']) > 0})
    if not ids:
        return
    matches = defaultdict(list)
    for start in range(0, len(ids), 80):
        group = ','.join(map(str, ids[start:start+80]))
        query = ('select n.dr2_source_id, n.dr3_source_id, n.angular_distance, g.ruwe, g.parallax '
                 'from gaiadr3.dr2_neighbourhood n join gaiadr3.gaia_source g on n.dr3_source_id=g.source_id '
                 f'where n.dr2_source_id in ({group})')
        failures = []
        for endpoint in GAIA:
            try:
                response = client.post(endpoint, data={'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'csv', 'QUERY': query},
                                       timeout=(8, 36))
                response.raise_for_status()
                parsed = csv.DictReader(io.StringIO(response.text))
                if 'ruwe' not in (parsed.fieldnames or []):
                    raise ValueError('Gaia TAP CSV header missing ruwe')
                for row in parsed:
                    matches[row['dr2_source_id']].append(row)
                break
            except (requests.RequestException, ValueError, KeyError) as exc:
                failures.append(str(exc))
        else:
            print('Gaia DR3 temporarily unavailable: ' + ' | '.join(failures), flush=True)
    for item in rows.values():
        options = matches[str(item.get('gaia_dr2'))]
        if len(options) == 1:
            row = options[0]
            item.update(gaia_dr3=row['dr3_source_id'], ruwe=float(row['ruwe']) if row['ruwe'] else None,
                        parallax_mas=float(row['parallax']) if row['parallax'] else None)


def date_of(product_row):
    return datetime.strptime(product_row['filename'][4:17], '%Y%j%H%M%S')


def eligible(star):
    mag, radius, gravity, dist = [star.get(key) for key in ('tmag', 'radius', 'logg', 'distance_pc')]
    return (None not in (mag, radius, gravity, dist) and mag < 11.5 and 0 < radius <= 1.2
            and gravity >= 4.0 and 0 < dist <= 300 and star.get('lumclass') in ('DWARF', None))


def eligible_hidden_gem(star):
    """Hidden Gems M/K dwarf filter for known planet / TOI hosts."""
    mag, radius, gravity, dist = [star.get(key) for key in ('tmag', 'radius', 'logg', 'distance_pc')]
    return (None not in (mag, radius, gravity, dist) and mag < 13.0 and 0 < radius <= .85
            and gravity >= 4.0 and 0 < dist <= 300 and star.get('lumclass') in ('DWARF', None))


def known_planet_hosts(client, cache):
    """Fetch and cache TIC IDs of confirmed exoplanet and TOI (CP/KP/PC) hosts from NASA TAP."""
    path = cache / 'known_hosts.json'
    if path.is_file():
        return set(read_json(path)['tic_ids'])
    queries = (
        ('tic_id', 'select tic_id from ps where default_flag=1 and tic_id is not null', None),
        ('tid', 'select tid,tfopwg_disp from toi where tid is not null', {'CP', 'KP', 'PC'}),
    )
    hosts = set()
    for field, query, allowed_disp in queries:
        response = client.get(TAP, params={'query': query, 'format': 'csv'}, timeout=(8, 60))
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text.lstrip('\ufeff')))
        if field not in (reader.fieldnames or ()):
            raise NetworkBatch(f'Unexpected TAP header for known_planet_hosts: {reader.fieldnames}')
        for row in reader:
            if allowed_disp is not None and (row.get('tfopwg_disp') or '').strip() not in allowed_disp:
                continue
            raw = (row.get(field) or '').strip().removeprefix('TIC ')
            if raw.isdigit():
                hosts.add(str(int(raw)))
    atomic_json(path, {'fetched_utc': datetime.now(timezone.utc).isoformat(),
                       'count': len(hosts), 'tic_ids': sorted(hosts, key=int)})
    return hosts


def hidden_gems_targets(client, cache, scanned, sectors):
    """Yield cross-year multi-sector M/K dwarf hosts of confirmed planets / TOIs for companion search."""
    latest = sectors[-1]
    hosts = known_planet_hosts(client, cache)
    resolved = {}
    metadata_path = cache / 'tic_v21.json'

    def load(number):
        with session() as isolated:
            return number, sector_products(isolated, cache, number)

    for recent in reversed(sectors):
        if recent < 27:
            break
        use = {recent}
        use.update(s for s in sectors if any(abs(s-(recent-gap)) <= 2 for gap in (27, 54, 81)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            for number, rows in pool.map(load, sorted(use - set(resolved))):
                resolved[number] = rows
        available = defaultdict(list)
        current = (set(resolved[recent]) & hosts) - scanned - {'261136679', '150428135'}
        for number in sorted(use):
            for tic in current & resolved[number].keys():
                available[tic].append(resolved[number][tic])
        good = {}
        for tic, rows in available.items():
            if len(rows) >= 3 and (date_of(rows[-1]) - date_of(rows[0])).days >= 731:
                good[tic] = [rows[0], rows[len(rows)//2], rows[-1]]
        if good:
            stars = metadata(client, metadata_path, good)
            gems = [(tic, star) for tic, star in stars.items() if eligible_hidden_gem(star)]
            gems.sort(key=lambda pair: (pair[1]['contamination'] is None, pair[1]['contamination'] or 0,
                                        pair[1]['radius'], pair[1]['tmag'], int(pair[0])))
            print(f'Hidden Gems Sector {recent}: {len(good)} known-host cross-year sets, '
                  f'{len(gems)} eligible M/K hosts', flush=True)
            for tic, star in gems:
                yield {'tic': int(tic), 'products': good[tic], 'stellar': star,
                       'latest_public_sector': latest, 'pool': 'hidden_gems'}


def select_science_batch(client, cache, blocked, sectors, pool='hybrid', total=100, hidden_quota=50):
    """Assemble a science batch under 'hybrid' (default), 'hidden_gems', or 'standard' pool."""
    selected = []
    if pool in ('hybrid', 'hidden_gems'):
        quota = total if pool == 'hidden_gems' else min(hidden_quota, total)
        for target in hidden_gems_targets(client, cache, blocked, sectors):
            blocked.add(str(target['tic']))
            selected.append(target)
            if len(selected) >= quota:
                break
    if len(selected) < total:
        for target in science_targets(client, cache, blocked, sectors):
            blocked.add(str(target['tic']))
            selected.append({**target, 'pool': 'standard'})
            if len(selected) >= total:
                break
    return selected


def science_targets(client, cache, scanned, sectors):
    latest = sectors[-1]
    resolved = {}
    metadata_path = cache / 'tic_v21.json'

    def load(number):
        with session() as isolated:
            return number, sector_products(isolated, cache, number)

    # An observing year has roughly 13.5 sectors. The same hemisphere repeats every two years.
    for recent in reversed(sectors):
        if recent < 27:
            break
        use = {recent}
        use.update(s for s in sectors if any(abs(s-(recent-gap)) <= 2 for gap in (27, 54, 81)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            for number, rows in pool.map(load, sorted(use - set(resolved))):
                resolved[number] = rows
        available = defaultdict(list)
        current = set(resolved[recent]) - scanned - {'261136679', '150428135'}
        for number in sorted(use):
            for tic in current & resolved[number].keys():
                available[tic].append(resolved[number][tic])
        good = {}
        for tic, rows in available.items():
            if len(rows) >= 3 and (date_of(rows[-1]) - date_of(rows[0])).days >= 731:
                good[tic] = [rows[0], rows[len(rows)//2], rows[-1]]
        if good:
            stars = metadata(client, metadata_path, good)
            print(f'Sector {recent}: {len(good)} cross-year product sets, '
                  f'{sum(eligible(star) for star in stars.values())} eligible TIC stars', flush=True)
            for tic, star in stars.items():
                if eligible(star):
                    yield {'tic': int(tic), 'products': good[tic], 'stellar': star, 'latest_public_sector': latest}


def controls(client, cache):
    prior = cache / 'controls.json'
    if prior.is_file():
        return read_json(prior)
    candidates = control_candidates(client, cache)
    preferred = ('WASP-18 b', 'WASP-4 b', 'WASP-19 b', 'WASP-121 b', 'WASP-6 b', 'WASP-43 b')
    candidates.sort(key=lambda row: preferred.index(row['pl_name']) if row['pl_name'] in preferred else len(preferred))
    preferred_rows = [row for row in candidates if row['pl_name'] in preferred]
    truths, products = [], []
    for row in preferred_rows + candidates[:10]:
        tic = int(row['tic_id'].removeprefix('TIC '))
        if any(record['tic'] == tic for record in truths):
            continue
        variants = []
        for number in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 27, 28, 29, 30, 31, 32, 33):
            rows = products_for_sector(client, tic, number)
            if rows:
                variants.append(rows[-1])
            if len(variants) == 3:
                break
        if len(variants) >= 2:
            truths.append({'tic': tic, 'planet': row['pl_name'], 'period_days': float(row['pl_orbper']),
                           'epoch_btjd': float(row['pl_tranmid']) - 2457000})
            products.append({'tic': tic, 'products': variants, 'stellar': {}})
        if len(truths) == 2:
            break
    if len(truths) != 2:
        raise NetworkBatch('Could not select two independent control planets')
    selection = {'truth': truths, 'targets': products}
    atomic_json(prior, selection)
    return selection


def eb_catalog(client, path):
    if not path.exists():
        response = client.get(EB_URL, timeout=(8, 45))
        response.raise_for_status()
        scratch = path.with_suffix('.part')
        scratch.write_text(response.text, encoding='utf-8')
        scratch.replace(path)
    records = csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig')))
    required = {'tess_id', 'period'}
    if not required <= set(records.fieldnames or []):
        raise NetworkBatch(f'Unexpected TESS-EB columns: {records.fieldnames}')
    grouped = defaultdict(list)
    for row in records:
        if row['tess_id'].strip().isdigit() and row['period'].strip():
            grouped[int(row['tess_id'])].append(row)
    return grouped
