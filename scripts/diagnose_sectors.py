import json
from pathlib import Path
import sys

import numpy as np

from golden_calibration import detrend, read_curve

root = Path(sys.argv[1])
items = json.loads((root / 'tess-golden-inputs.json').read_text())
if sys.argv[2:] == ['--smoke']:
    from tempfile import TemporaryDirectory

    from tess_campaign import candidate_outputs, diagnose, process_target
    from tess_sources import session
    from tess_vetting import catalogs, match_catalog

    tic = 150428135
    pair = [row for row in items if row['tic'] == tic and row['sector'] in (4, 5)]
    record = process_target(root, pair)
    with session() as client:
        reference = catalogs(client, [tic])
    assert len(reference[tic]) >= 4
    assert record['clean_points'] > 20000
    test_rows = [{**row, 'tic': tic, 'catalog': match_catalog(row, reference[tic]),
                  'vet': {**row['vet'], 'pixel': {'state': 'INCONCLUSIVE', 'reason': 'Smoke test'}}}
                 for row in record['candidates']]
    with TemporaryDirectory(dir=root.parent / 'tmp') as workspace:
        output = Path(workspace)
        candidate_outputs(output, test_rows)
        if test_rows:
            path = diagnose(output, root, record, test_rows[0], 'smoke')
            assert (output / path).read_bytes().startswith(b'\x89PNG')
    print(f'{record["clean_points"]} clean cadences, {len(test_rows)} candidate signals, {len(reference[tic])} catalog matches')
    raise SystemExit(0)

for item in items:
    if item['tic'] != 261136679:
        continue
    t, y, dy = detrend(*read_curve(root, item))
    median = np.median(y)
    mad = 1.4826 * np.median(np.abs(y - median))
    print(item['sector'], len(t), np.round(np.percentile(y, [0, .01, .1, 1, 10, 90, 99]) * 1e6, 1),
          'mad ppm', round(mad * 1e6, 1), 'median error', round(np.median(dy)*1e6, 1),
          'below -8 MAD', sum(y < median - 8 * mad), 'above 4 MAD', sum(y > median + 4 * mad), flush=True)
