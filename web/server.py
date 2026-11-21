"""Read-only local TESS Observatory Dashboard server (127.0.0.1:18805)."""

from collections import Counter
import csv
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlparse


WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent
V21_DIR = (PROJECT_DIR / 'outputs' / 'v21').resolve()
TRIAGE_PATH = WEB_DIR / 'triage_notes.json'
HOST = '127.0.0.1'
PORT = 18805
ALLOWED_STATIC_EXTS = {'.png', '.json', '.csv', '.md'}
BATCH_RE = re.compile(r'^v21_batch_\d+$')


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def read_text(path: Path, default=''):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return default


def load_triage() -> dict:
    data = read_json(TRIAGE_PATH, {})
    return data if isinstance(data, dict) else {}


def save_triage_entry(key: str, status: str, note: str) -> dict:
    data = load_triage()
    if status == 'unreviewed' and not note.strip():
        data.pop(key, None)
    else:
        data[key] = {
            'status': status,
            'note': note.strip()[:500],
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
    tmp = TRIAGE_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(TRIAGE_PATH)
    return data


def list_batch_dirs():
    if not V21_DIR.is_dir():
        return []
    dirs = [p for p in V21_DIR.iterdir() if p.is_dir() and BATCH_RE.match(p.name)]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def clean_candidate(item: dict, batch_name: str, triage: dict) -> dict:
    vet_raw = item.get('vet') or {}
    vet = {}
    for k, v in vet_raw.items():
        if isinstance(v, dict):
            vet[k] = {vk: vv for vk, vv in v.items() if vk != 'difference_image'}
        else:
            vet[k] = v
    stellar = vet.get('stellar_radius_ruwe') or {}
    catalog = item.get('catalog') or {}
    diag = item.get('diagnostic')
    diag_url = f'/files/{batch_name}/{diag}' if diag else None
    period = item.get('period_days')
    epoch = item.get('epoch_btjd')
    key = f"{batch_name}:{item.get('tic')}:{item.get('kind')}:{round(period, 4) if period else round(epoch or 0, 3)}"
    return {
        'key': key,
        'batch': batch_name,
        'tic': item.get('tic'),
        'sectors': item.get('sectors') or [],
        'kind': item.get('kind', 'periodic'),
        'period_days': period,
        'epoch_btjd': epoch,
        'duration_hours': item.get('duration_hours') or ((item.get('duration_days') or 0) * 24 or None),
        'depth_ppm': item.get('depth_ppm'),
        'snr': item.get('snr'),
        'sde': item.get('sde') if item.get('sde') is not None else item.get('mad_sde'),
        'rp_rearth': stellar.get('rp_rearth'),
        'radius_rsun': stellar.get('radius_rsun'),
        'gaia_ruwe': stellar.get('gaia_ruwe'),
        'catalog_state': catalog.get('state', 'UNKNOWN'),
        'catalog': catalog,
        'high_priority': bool(item.get('high_priority')),
        'diagnostic': diag,
        'diagnostic_url': diag_url,
        'vet': vet,
        'triage': triage.get(key, {'status': 'unreviewed', 'note': ''}),
    }


def build_batches():
    batches = []
    for bdir in list_batch_dirs():
        st = read_json(bdir / 'batch_status.json', {})
        if not st:
            continue
        hp = read_json(bdir / 'high_priority.json', []) or []
        hp_periodic = sum(1 for c in hp if c.get('kind') == 'periodic')
        hp_single = sum(1 for c in hp if c.get('kind') == 'single')
        pngs = sorted(p.name for p in bdir.glob('candidate_*.png'))
        blind = st.get('blind') or []
        blind_ok = sum(1 for b in blind if b.get('recovered'))
        mtime = datetime.fromtimestamp((bdir / 'batch_status.json').stat().st_mtime, tz=timezone.utc).isoformat(timespec='seconds')
        batches.append({
            'batch': bdir.name,
            'updated_utc': mtime,
            'searched': st.get('searched', 0),
            'science': st.get('science', 0),
            'candidates': st.get('candidates', 0),
            'pixel_reviews': st.get('pixel_reviews', 0),
            'high_priority': st.get('high_priority', len(hp)),
            'hp_periodic': hp_periodic,
            'hp_single': hp_single,
            'blind_ok': blind_ok,
            'blind_total': len(blind),
            'blind_pass': bool(st.get('blind_pass')),
            'blind': blind,
            'png_count': len(pngs),
            'target_pool': st.get('target_pool'),
            'hidden_gems_count': st.get('hidden_gems_count'),
        })
    return batches


def build_overview():
    sync = read_json(V21_DIR / 'sync_status.json', {}) or {}
    mon = read_json(V21_DIR / 'v21_monitor.json', {}) or {}
    golden = read_json(V21_DIR / 'v21_golden' / 'golden_report.json', {}) or {}
    stop_reason = read_text(V21_DIR / 'STOP_REASON.txt', '').strip() or sync.get('stop_reason')
    batches = build_batches()

    total_science = sum(b['science'] for b in batches)
    total_candidates = sum(b['candidates'] for b in batches)
    total_hp = sum(b['high_priority'] for b in batches)
    total_hp_periodic = sum(b['hp_periodic'] for b in batches)
    total_hp_single = sum(b['hp_single'] for b in batches)
    blind_ok = sum(b['blind_ok'] for b in batches)
    blind_total = sum(b['blind_total'] for b in batches)

    golden_matches = sum(len(t.get('matches') or []) for t in (golden.get('targets') or []))
    deep_vet = build_deep_vet(include_rows=False)

    return {
        'sync': sync,
        'monitor': mon,
        'stop_reason': stop_reason,
        'golden': {
            'passed': bool(golden.get('passed')),
            'sha256': (golden.get('search_code_sha256') or '')[:12],
            'matches': golden_matches,
            'single_injection': bool((golden.get('single_injection') or {}).get('passed')),
            'elapsed_seconds': golden.get('elapsed_seconds'),
        },
        'kpis': {
            'batches_completed': len(batches),
            'science_targets': total_science,
            'total_candidates': total_candidates,
            'high_priority_total': total_hp,
            'high_priority_periodic': total_hp_periodic,
            'high_priority_single': total_hp_single,
            'blind_ok': blind_ok,
            'blind_total': blind_total,
            'deep_vet_grades': deep_vet['grade_counts'],
            'deep_dives_count': len(deep_vet['deep_dives']),
        },
        'batches': batches,
    }


def build_candidates(params: dict):
    triage = load_triage()
    batch_filter = (params.get('batch') or ['all'])[0]
    kind_filter = (params.get('kind') or ['all'])[0]
    hp_filter = (params.get('hp') or ['all'])[0]
    cat_filter = (params.get('catalog') or ['all'])[0]
    triage_filter = (params.get('triage') or ['all'])[0]
    q = (params.get('q') or [''])[0].strip().lower()
    try:
        min_snr = float((params.get('min_snr') or ['0'])[0] or 0)
    except ValueError:
        min_snr = 0.0
    try:
        min_sde = float((params.get('min_sde') or ['0'])[0] or 0)
    except ValueError:
        min_sde = 0.0

    rows = []
    target_dirs = list_batch_dirs()
    if batch_filter != 'all':
        target_dirs = [d for d in target_dirs if d.name == batch_filter]

    for bdir in target_dirs:
        fname = 'high_priority.json' if hp_filter == '1' else 'candidates_ranked.json'
        items = read_json(bdir / fname, []) or []
        for it in items:
            c = clean_candidate(it, bdir.name, triage)
            if kind_filter != 'all' and c['kind'] != kind_filter:
                continue
            if hp_filter == '1' and not c['high_priority']:
                continue
            if hp_filter == '0' and c['high_priority']:
                continue
            if cat_filter != 'all' and c['catalog_state'] != cat_filter:
                continue
            if triage_filter != 'all' and c['triage'].get('status', 'unreviewed') != triage_filter:
                continue
            if (c['snr'] or 0) < min_snr:
                continue
            if min_sde > 0 and c['kind'] == 'periodic' and (c['sde'] or 0) < min_sde:
                continue
            if q and q not in str(c['tic']) and q not in c['batch'].lower():
                continue
            rows.append(c)

    rows.sort(key=lambda r: (not r['high_priority'], r['batch'] != (target_dirs[0].name if target_dirs else ''), -(r['snr'] or 0)))
    return {'total': len(rows), 'items': rows[:500]}


def build_deep_vet(include_rows=True):
    dv_dir = V21_DIR / 'deep_vet_top10'
    csv_path = dv_dir / 'MASTER_HITS_deep_vetted.csv'
    grade_counts = Counter()
    reason_counts = Counter()
    rows = []
    if csv_path.is_file():
        with csv_path.open('r', encoding='utf-8-sig', newline='') as fh:
            for r in csv.DictReader(fh):
                grade = r.get('final_grade', 'UNKNOWN')
                reason = r.get('first_reason', '') or 'NONE'
                grade_counts[grade] += 1
                reason_counts[reason] += 1
                if include_rows:
                    rows.append({
                        'id': r.get('id'),
                        'batch': r.get('batch'),
                        'tic': r.get('tic'),
                        'sectors': r.get('sectors'),
                        'period_days': float(r['period_days']) if r.get('period_days') else None,
                        'depth_ppm': float(r['depth_ppm']) if r.get('depth_ppm') else None,
                        'snr': float(r['snr']) if r.get('snr') else None,
                        'clean_snr': float(r['clean_snr']) if r.get('clean_snr') else None,
                        'kepler_ratio': float(r['duration_to_keplerian_ratio']) if r.get('duration_to_keplerian_ratio') else None,
                        'u_vs_smooth_bic': float(r['u_vs_smooth_delta_bic']) if r.get('u_vs_smooth_delta_bic') else None,
                        'final_grade': grade,
                        'first_reason': reason,
                        'stellar_variability': r.get('stellar_variability'),
                        'artifact_tags': r.get('artifact_tags'),
                    })

    deep_dives = []
    if V21_DIR.is_dir():
        for d in sorted(V21_DIR.glob('deep_dive_*')):
            if not d.is_dir():
                continue
            reports = sorted(d.glob('REPORT_*.md'))
            pngs = sorted(d.glob('DIAGNOSTIC_*.png'))
            ev = sorted(d.glob('evidence_*.json'))
            report_text = read_text(reports[0]) if reports else ''
            ev_data = read_json(ev[0], {}) if ev else {}
            deep_dives.append({
                'name': d.name,
                'tic': d.name.replace('deep_dive_', ''),
                'verdict': ev_data.get('verdict') or ev_data.get('final_verdict') or 'ARCHIVED',
                'report_file': reports[0].name if reports else None,
                'report_markdown': report_text[:12000],
                'diagnostic_url': f'/files/{d.name}/{pngs[0].name}' if pngs else None,
            })

    return {
        'grade_counts': dict(grade_counts),
        'reason_counts': dict(reason_counts),
        'report_markdown': read_text(dv_dir / 'DEEP_VET_REPORT.md')[:12000] if include_rows else '',
        'rows': rows if include_rows else [],
        'deep_dives': deep_dives,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=HTTPStatus.OK):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path):
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, 'Not found')
            return
        mime, _ = mimetypes.guess_type(str(path))
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', mime or 'application/octet-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            return self.send_file(WEB_DIR / 'index.html')
        if path in ('/style.css', '/app.js'):
            return self.send_file(WEB_DIR / path.lstrip('/'))
        if path == '/api/overview':
            return self.send_json(build_overview())
        if path == '/api/batches':
            return self.send_json({'batches': build_batches()})
        if path == '/api/candidates':
            return self.send_json(build_candidates(params))
        if path == '/api/deep-vet':
            return self.send_json(build_deep_vet(include_rows=True))
        if path == '/api/triage':
            return self.send_json(load_triage())
        if path.startswith('/files/'):
            rel = unquote(path[len('/files/'):])
            if '..' in Path(rel).parts or rel.startswith(('/', '\\')):
                return self.send_error(HTTPStatus.FORBIDDEN, 'Forbidden path')
            target = (V21_DIR / rel).resolve()
            if not target.is_relative_to(V21_DIR) or target.suffix.lower() not in ALLOWED_STATIC_EXTS:
                return self.send_error(HTTPStatus.FORBIDDEN, 'Forbidden path')
            return self.send_file(target)
        self.send_error(HTTPStatus.NOT_FOUND, 'Not found')

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/triage':
            return self.send_error(HTTPStatus.NOT_FOUND, 'Not found')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 16384:
                return self.send_json({'error': 'Invalid payload size'}, HTTPStatus.BAD_REQUEST)
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            key = str(body.get('key', '')).strip()
            status = str(body.get('status', 'unreviewed')).strip()
            note = str(body.get('note', ''))
            if not key or status not in ('unreviewed', 'deep_dive', 'promising', 'rejected'):
                return self.send_json({'error': 'Invalid key or status'}, HTTPStatus.BAD_REQUEST)
            data = save_triage_entry(key, status, note)
            return self.send_json({'ok': True, 'entry': data.get(key, {'status': 'unreviewed', 'note': ''})})
        except (ValueError, OSError) as exc:
            return self.send_json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f'TESS Observatory Dashboard listening on http://{HOST}:{PORT}/ (read-only V21={V21_DIR})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
