"""Public TESS product discovery and checked downloads on the Jetson."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

from astropy.io import fits
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ARCHIVE = 'https://s3.amazonaws.com/stpubdata'
MAST = 'https://mast.stsci.edu/api/v0/invoke'
TAP = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync'
ROOT = Path('/home/data/astro_tess')


def session():
    client = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=('GET', 'HEAD', 'POST'), respect_retry_after_header=True)
    client.mount('https://', HTTPAdapter(max_retries=retry))
    return client


def atomic_json(path, value):
    scratch = path.with_suffix(path.suffix + '.part')
    scratch.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    scratch.replace(path)


def digest(path):
    with path.open('rb') as stream:
        checksum = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
        return checksum.hexdigest()


def mast(client, service, columns, filters, page=1, page_size=2000):
    req = {'service': service, 'params': {'columns': columns, 'filters': filters},
           'format': 'json', 'page': page, 'pagesize': page_size}
    response = client.post(MAST, data={'request': json.dumps(req)}, timeout=(8, 45))
    response.raise_for_status()
    result = response.json()
    if result.get('status') != 'COMPLETE' or not isinstance(result.get('data'), list):
        raise RuntimeError(f'MAST {service} page {page}: {result.get("status")} {result.get("msg")}')
    return result


def product(tic, sector, obs_id, suffix='lc'):
    tid = f'{tic:016d}'
    target_path = '/'.join(tid[i:i+4] for i in range(0, 16, 4))
    basename = f'{obs_id}_{suffix}.fits'
    return {'tic': tic, 'sector': sector, 'filename': basename,
            'url': f'{ARCHIVE}/tess/public/tid/s{sector:04d}/{target_path}/{basename}'}


def products_for_sector(client, tic, sector):
    placeholder = product(tic, sector, 'placeholder')
    prefix = placeholder['url'].removeprefix(ARCHIVE + '/').rsplit('/', 1)[0] + '/'
    response = client.get(ARCHIVE, params={'list-type': 2, 'prefix': prefix, 'max-keys': 50}, timeout=(8, 30))
    response.raise_for_status()
    document = ET.fromstring(response.content)
    rows = []
    for obj in document.findall('{*}Contents'):
        key = obj.findtext('{*}Key')
        if key.endswith('-s_lc.fits'):
            rows.append({'tic': tic, 'sector': sector, 'filename': key.rsplit('/', 1)[-1],
                         'url': f'{ARCHIVE}/{key}', 'size': int(obj.findtext('{*}Size'))})
    return sorted(rows, key=lambda row: row['filename'])


def download(client, row, directory, remaining):
    target = directory / row['filename']
    if target.is_file():
        if 'size' in row and target.stat().st_size != row['size']:
            raise ValueError(f'Unexpected cached file size: {target.name}')
        return {**row, 'size': target.stat().st_size, 'sha256': digest(target)}
    scratch = target.with_suffix(target.suffix + '.part')
    bytes_written = 0
    with client.get(row['url'], stream=True, timeout=(8, 45)) as response:
        response.raise_for_status()
        expected = int(response.headers['Content-Length'])
        if not 0 < expected <= 300 * 1024**2 or expected % 2880 or expected != row.get('size', expected):
            raise ValueError(f'Unexpected product size for {target.name}: {expected}')
        if callable(remaining):
            remaining(expected)
        elif expected > remaining:
            raise BudgetExceeded(f'{target.name} needs {expected:,} bytes, remaining {remaining:,}')
        checksum = sha256()
        with scratch.open('wb') as stream:
            for data in response.iter_content(chunk_size=256 * 1024):
                bytes_written += len(data)
                if bytes_written > expected:
                    raise ValueError(f'Oversized response: {target.name}')
                stream.write(data)
                checksum.update(data)
    if bytes_written != expected:
        raise ValueError(f'Truncated response: {target.name}')
    with fits.open(scratch, memmap=True) as hdus:
        if hdus[0].header['TICID'] != row['tic'] or hdus[0].header['SECTOR'] != row['sector']:
            raise ValueError(f'Unexpected FITS identity: {target.name}')
        hdus.verify('exception')
    scratch.replace(target)
    return {**row, 'size': bytes_written, 'sha256': checksum.hexdigest()}


class BudgetExceeded(RuntimeError):
    pass


def expanded_golden():
    original = ROOT / 'golden'
    expanded = ROOT / 'golden_expanded'
    expanded.mkdir(exist_ok=True)
    if (expanded / 'tess-golden-inputs.json').is_file():
        return
    manifest = json.loads((original / 'tess-golden-inputs.json').read_text(encoding='utf-8'))
    for row in manifest:
        target = expanded / row['filename']
        if not target.exists():
            target.hardlink_to(original / row['filename'])
    shutil.copy2(original / 'golden-ephemerides.json', expanded)
    with session() as client:
        sectors = []
        for sector in range(2, 14):
            rows = products_for_sector(client, 261136679, sector)
            if rows:
                row = download(client, rows[-1], expanded, 32 * 1024**2)
                manifest.append(row)
                sectors.append(sector)
                print(f'π Men: verified sector {sector}', flush=True)
            if len(sectors) == 4:
                break
    if len(sectors) < 2:
        raise RuntimeError(f'Insufficient new π Men sectors: {sectors}')
    atomic_json(expanded / 'tess-golden-inputs.json', manifest)


if __name__ == '__main__':
    if sys.argv[1:] != ['golden']:
        raise SystemExit('Usage: tess_sources.py golden')
    expanded_golden()
