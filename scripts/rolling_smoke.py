"""Jetson-only tests of the public transfer and the rolling stop paths."""

from argparse import Namespace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from rolling_scanner import cleanup, commit, resource_issue, run, supervise
from rolling_sources import GIB, Ledger, NetworkBatch, eb_catalog, fetch_fits, gaia_metadata
from tess_sources import atomic_json, session


def resource_cases(root):
    meminfo = 'SwapTotal: 4194304 kB\nSwapFree: 4194304 kB\n'
    fake_sensor = Path('/sys/class/thermal/thermal_zone0/temp')
    with patch('rolling_scanner.shutil.disk_usage', side_effect=[Namespace(free=24*GIB)]):
        assert 'eMMC' in resource_issue(root, None)[0]
    with patch('rolling_scanner.shutil.disk_usage', side_effect=[Namespace(free=30*GIB), Namespace(free=99*GIB)]):
        assert 'NVMe' in resource_issue(root, None)[0]
    with patch('rolling_scanner.shutil.disk_usage', return_value=Namespace(free=120*GIB)), \
            patch.object(Path, 'read_text', return_value=meminfo.replace('SwapFree: 4194304', 'SwapFree: 2000000')):
        assert 'Swap' in resource_issue(root, None)[0]
    with patch('rolling_scanner.shutil.disk_usage', return_value=Namespace(free=120*GIB)), \
            patch.object(Path, 'glob', return_value=[fake_sensor]), \
            patch.object(Path, 'read_text', side_effect=[meminfo, '81000']):
        assert '80°C' in resource_issue(root, time.monotonic()-61)[0]
    print('Resource guards verified: root, NVMe, swap, sustained temperature', flush=True)


def stop_cases(root):
    (root / 'STOP').touch()
    args = Namespace(root=root, mode='stop_on_hit', max_batches=5, workers=1)
    with patch('rolling_scanner.preflight'):
        assert supervise(args) == 0
    assert 'STOP sentinel' in (root / 'STOP_REASON.txt').read_text()
    (root / 'STOP').unlink()
    with patch('rolling_scanner.preflight'), patch('rolling_scanner.calibration', return_value='sha'), \
            patch('rolling_scanner.execute_batch', side_effect=NetworkBatch('simulated download failure')), \
            patch('rolling_scanner.time.sleep'):
        try:
            run(args)
        except RuntimeError as exc:
            assert 'Three consecutive' in str(exc)
        else:
            raise AssertionError('Network failure guard did not stop')
    state_file = root / 'state/scanner_state.json'
    state = json.loads(state_file.read_text())
    assert state['failures'] == 3
    directory = root / 'out/v21_batch_00001'
    directory.mkdir(parents=True)
    atomic_json(directory / 'high_priority.json', [])
    report = {'batch': directory.name, 'blind_pass': False, 'blind': [{'tic': 1}]}
    with patch('rolling_scanner.publish'):
        assert 'Blind recovery failed' in commit(args, report, [], state, state_file)
    print('STOP, three consecutive failure and blind recovery guards verified', flush=True)


def download_case(root, golden):
    raw = root / 'raw/v21_batch_00001'
    raw.mkdir(parents=True)
    manifest = json.loads((golden / 'tess-golden-inputs.json').read_text())
    item = next(row for row in manifest if row['tic'] == 261136679 and row['sector'] == 1)
    fragment = (raw / item['filename']).with_suffix('.aws.part')
    with session() as client:
        with client.head(item['url'], timeout=(8, 20)) as response:
            response.raise_for_status()
            headers = response.headers
            identity = {'url': item['url'], 'size': int(headers['Content-Length']),
                        'etag': headers['ETag'].strip('"'), 'last_modified': headers.get('Last-Modified')}
        response = client.get(item['url'], headers={'Range': 'bytes=0-65535'}, timeout=(8, 20))
        assert response.status_code == 206 and len(response.content) == 65536
        fragment.write_bytes(response.content)
        atomic_json(fragment.with_suffix('.part.json'), identity)
        ledger = Ledger(root / 'ledger.json')
        product = fetch_fits(client, item, raw, ledger)
        assert product['sha256'] == item['sha256'] and product['resumed_from'] == 65536
        assert ledger.used == item['size'] - 65536
        row = {150428135: {'gaia_dr2': '5284517766615492736'}}
        gaia_metadata(client, row)
        assert row[150428135].get('ruwe') is not None
        binaries = eb_catalog(client, root / 'eb.csv')
        assert sum(map(len, binaries.values())) > 4000
    assert cleanup(root, raw, [{'tic': item['tic'], 'products': [item]}], {item['tic']}) == 0
    assert cleanup(root, raw, [{'tic': item['tic'], 'products': [item]}], set()) >= 2
    assert not (raw / item['filename']).exists()
    print(f'AWS Range resume SHA-256, Gaia DR3, TESS-EB and scoped retention verified; RUWE={row[150428135]["ruwe"]}', flush=True)


if __name__ == '__main__':
    base = Path(sys.argv[1])
    with TemporaryDirectory(prefix='v21-smoke-', dir=base / 'tmp') as workspace:
        root = Path(workspace)
        resource_cases(root)
        stop_cases(root)
        download_case(root, base / 'golden_expanded')
