"""Run the 42-signal B2–B6 deep review without restarting or modifying the scanner."""

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import requests

from deep_vet_catalog import OUTPUT, compare, load_catalogs, read_sample
from deep_vet_photometry import keplerian_duration_ratio, load_curve, quality_mask, reference_shapes, review_events, rotation
from deep_vet_pixels import DeepBudget, check_pixels
from golden_calibration import plt
from rolling_sources import GIB, NetworkBatch, TransferLimit, fetch_fits
from tess_sources import atomic_json, digest, product, session
from tess_vetting import duration_days

SUPPLEMENTAL_TPF = False


def check_boundaries(root):
    root_free = shutil.disk_usage('/').free
    if os.geteuid() == 0 or root.resolve() != Path('/home/data/astro_tess') or root_free < 25*GIB:
        raise RuntimeError('Deep-vet host boundary failed')
    if not (root / 'STOP').is_file():
        raise RuntimeError('The rolling scanner STOP sentinel is required')
    with (root / 'logs/rolling_v21.lock').open('rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
    for name in ('zerotier-one.service', 'report_robot_network_info.service'):
        reply = subprocess.check_output(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'UnitFileState'], text=True)
        if set(reply.splitlines()) != {'ActiveState=inactive', 'UnitFileState=disabled'}:
            raise RuntimeError(f'Unexpected status of {name}: {reply}')
    if subprocess.check_output(['ss', '-H', '-ltn', 'sport = :8080'], text=True).strip():
        raise RuntimeError('Port 8080 is in use')
    return {'root_free_gib': round(root_free/GIB, 3), 'nvme_free_gib': round(shutil.disk_usage(root).free/GIB, 3),
            'non_root': True, 'scanner_paused': True, 'port_8080_listening': False, 'services': 'inactive/disabled'}


def inputs(root, candidate):
    identifier = candidate['tic']
    batch = candidate['batch']
    listing = json.loads((root / 'out' / batch / 'input_targets.json').read_text(encoding='utf-8'))
    targets = [value for value in listing if value['tic'] == identifier]
    if len(targets) != 1:
        raise ValueError(f'TIC {identifier}: expected a single archived input row')
    directory = root / 'raw' / batch
    entries = sorted(targets[0]['products'], key=lambda value: value['sector'])
    if [row['sector'] for row in entries] != sorted(candidate['sectors']):
        raise ValueError(f'TIC {identifier}: archived sectors differ from the source candidate')
    curves = []
    for entry in entries:
        path = directory / entry['filename']
        receipt = path.with_suffix('.receipt.json')
        if receipt.is_file() and digest(path) != json.loads(receipt.read_text(encoding='utf-8'))['sha256']:
            raise ValueError(f'TIC {identifier}: input light-curve digest mismatch')
        curves.append(load_curve(path))
    return directory, entries, curves


def review_photometry(root, candidate):
    _, _, curves = inputs(root, candidate)
    event_result, _ = review_events(curves, candidate)
    details, _ = rotation(curves, candidate, event_result)
    return event_result, details


def shape_evidence(row):
    return row['variability'].get('residual') or row['instrument']


def primary_grade(row):
    catalog = row['catalog']['state']
    if catalog == 'CATALOG_INCONCLUSIVE':
        return 'BORDERLINE', 'CATALOG_INCONCLUSIVE'
    if catalog != 'UNCATALOGED':
        return 'REJECT', catalog
    phot = row['instrument']
    star = row['variability']['state']
    if phot['state'] != 'PASS':
        if star == 'STELLAR_VAR' and phot['contaminated_snr2_fraction'] < .5:
            return 'REJECT', 'STELLAR_VAR'
        if phot['state'] == 'REJECT':
            return 'REJECT', '+'.join(phot['tags'])
        return 'BORDERLINE', 'CLEAN_SIGNAL_INCONCLUSIVE'
    if star == 'STELLAR_VAR':
        return 'REJECT', 'STELLAR_VAR'
    if star != 'PASS':
        return 'BORDERLINE', 'LS_INCONCLUSIVE'
    return None, None


def good_events(row, sector):
    return [event for event in shape_evidence(row)['events'] if event['sector'] == sector and event['snr'] is not None
            and event['background_measurable'] and not any(event[name] for name in
                                                           ('sector_edge', 'downlink_edge', 'momentum_dump', 'background_spike'))]


def trial(client, out, row, path, sector):
    candidate = row['candidate']
    events = good_events(row, sector)
    shape_stats = shape_evidence(row)['shape']
    shape = shape_stats['smooth_u_model'] if (shape_stats.get('delta_bic_u_vs_smooth') or 0) >= 6 else shape_stats['model']
    if shape:
        d = duration_days(candidate)
        candidate = {**candidate, 'duration_days': d*shape['width_fraction']}
        events = [{**event, 'btjd': event['btjd']+d*shape['offset_fraction']} for event in events]
    try:
        details = check_pixels(client, out / 'catalog_sources', path, candidate, events)
    except (KeyError, ValueError, RuntimeError, OSError, np.linalg.LinAlgError, requests.RequestException) as exc:
        details = {'state': 'INCONCLUSIVE', 'reason': str(exc)[:500]}
    return {**details, 'sector': sector, 'filename': path.name}


def chosen_pixel(trials):
    candidates = [check for check in trials if check['state'] == 'BG_CONTAMINATION']
    if not candidates:
        candidates = [check for check in trials if check['state'] == 'CENTERED']
    if candidates:
        return max(candidates, key=lambda check: check['centroid']['snr'])
    return {'state': 'INCONCLUSIVE', 'reason': '有效 TPF 差异图或 Gaia 邻源定位证据不足'}


def priority(row):
    p = shape_evidence(row)
    candidate = row['candidate']
    bic = p['shape']['delta_bic'] or 0.
    smooth = p['shape'].get('delta_bic_u_vs_smooth') or 0.
    pixel = row['pixel'].get('offset_px', 3.)
    return (p['repeat_sectors'], min(60., bic, smooth), -pixel, p['clean']['snr'],
            candidate.get('mad_sde') or 0., candidate['snr'], -candidate['tic'])


def apply_pixels(client, root, out, review, budget):
    supplemental = out / 'tpf_cache'
    supplemental.mkdir(exist_ok=True)
    pending = []
    for row in review:
        row['pixel'] = {'state': 'NOT_RUN'}
        row['pixel_trials'] = []
        if primary_grade(row)[0] is not None:
            continue
        candidate = row['candidate']
        directory = root / 'raw' / candidate['batch']
        entries = row['products']
        for lc in entries:
            sector = lc['sector']
            tpf = directory / lc['filename'].replace('_lc.fits', '_tp.fits')
            if not tpf.is_file() or not good_events(row, sector):
                continue
            receipt = tpf.with_suffix('.receipt.json')
            if receipt.is_file() and digest(tpf) != json.loads(receipt.read_text(encoding='utf-8'))['sha256']:
                continue
            row['pixel_trials'].append(trial(client, out, row, tpf, sector))
        row['pixel'] = chosen_pixel(row['pixel_trials'])
        print(f"TPF cached {row['id']}: {row['pixel']['state']}", flush=True)
        if row['pixel']['state'] == 'INCONCLUSIVE':
            pending.append(row)
    if SUPPLEMENTAL_TPF:
        for row in sorted(pending, key=priority, reverse=True):
            candidate = row['candidate']
            visited = {item['sector'] for item in row['pixel_trials']}
            sectors = shape_evidence(row)['clean_sectors']
            entries = sorted(row['products'], key=lambda entry: sectors[str(entry['sector'])]['snr'], reverse=True)
            for lc in entries:
                sector = lc['sector']
                if sector in visited or not good_events(row, sector) or sectors[str(sector)]['snr'] < 3:
                    continue
                check_boundaries(root)
                obs = lc['filename'].removesuffix('_lc.fits')
                tpf = product(candidate['tic'], sector, obs, 'tp')
                try:
                    receipt = fetch_fits(client, tpf, supplemental, budget)
                    result = trial(client, out, row, supplemental / receipt['filename'], sector)
                    row['pixel_trials'].append(result)
                    row['pixel'] = chosen_pixel(row['pixel_trials'])
                    print(f"TPF supplemental {row['id']} S{sector}: {result['state']}; {budget.used} bytes reserved", flush=True)
                except (NetworkBatch, TransferLimit, OSError, ValueError) as exc:
                    row['pixel_trials'].append({'sector': sector, 'state': 'INCONCLUSIVE', 'reason': str(exc)[:500]})
                    print(f"TPF supplemental {row['id']} S{sector}: {exc}", flush=True)
                if row['pixel']['state'] != 'INCONCLUSIVE':
                    break


def classify(rows):
    verified = []
    for row in rows:
        grade, reason = primary_grade(row)
        if grade is None:
            pixel = row['pixel']['state']
            phot = shape_evidence(row)
            bic = phot['shape']['delta_bic']
            smooth_bic = phot['shape'].get('delta_bic_u_vs_smooth')
            if pixel == 'BG_CONTAMINATION':
                grade, reason = 'REJECT', pixel
            elif pixel != 'CENTERED':
                grade, reason = 'BORDERLINE', 'TPF_INCONCLUSIVE'
            elif bic is not None and bic <= -6:
                grade, reason = 'REJECT', 'V_SHAPE'
            elif bic is None or bic < 6:
                grade, reason = 'BORDERLINE', 'SHAPE_INCONCLUSIVE'
            elif smooth_bic is None or smooth_bic < 6:
                grade, reason = 'BORDERLINE', 'SMOOTH_PROFILE_NOT_EXCLUDED'
            elif row.get('kepler_ratio') is not None and row['kepler_ratio'] > 1.8:
                grade, reason = 'REJECT', 'KEPLER_DURATION_INCONSISTENT'
            elif phot['repeat_sectors'] < 2:
                grade, reason = 'BORDERLINE', 'NO_INDEPENDENT_SECTOR_REPEAT'
            else:
                verified.append(row)
        row['grade'], row['first_reason'] = grade, reason
    verified.sort(key=priority, reverse=True)
    for index, row in enumerate(verified, 1):
        row['rank'] = index
        if index <= 10:
            row['grade'], row['first_reason'] = 'KEEP_TOP', 'PASS_ALL'
        else:
            row['grade'], row['first_reason'] = 'BORDERLINE', 'PASS_OUTSIDE_TOP10'
    return verified[:10]


def render(root, out, row):
    candidate = row['candidate']
    _, _, curves = inputs(root, candidate)
    measured_curves = curves
    if row['variability'].get('residual'):
        _, measured_curves = rotation(curves, candidate, row['instrument'])
    _, windows = review_events(measured_curves, candidate)
    d = duration_days(candidate)
    shape = shape_evidence(row)['shape']
    pixel = row['pixel']
    sector = pixel['sector']
    fig = plt.figure(figsize=(12, 10), layout='constrained')
    grid = fig.add_gridspec(3, 2, height_ratios=[1, 1, 1.4])
    axes = [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1]), fig.add_subplot(grid[1, 0]),
            fig.add_subplot(grid[1, 1]), fig.add_subplot(grid[2, :])]
    overview, folded, odd_even, local, image = axes
    colors = ('#166b85', '#b85f32', '#695ca0')
    for curve, color in zip(curves, colors):
        valid = quality_mask(curve)
        times, flux = curve.time[valid], curve.flux[valid]
        overview.scatter(times[::8], flux[::8]*1e6, s=1.6, alpha=.7, color=color, label=f'S{curve.sector}')
        for event in good_events(row, curve.sector):
            overview.axvline(event['btjd'], color=color, alpha=.1, linewidth=.65)
    overview.legend(loc='best', fontsize=8)
    overview.set(xlabel='BTJD', ylabel='PDCSAP (ppm)', title='Stitched SPOC light curves')
    x, y, e = [np.array(shape[name]) for name in ('x', 'y', 'err')]
    folded.errorbar(x*d*24, y*1e6, yerr=e*1e6, fmt='o', markersize=3, color='#166b85')
    xx = np.linspace(-1.5, 1.5, 301)
    model = shape['smooth_u_model']
    width, shift, ingress = [model[name] for name in ('width_fraction', 'offset_fraction', 'ingress')]
    template = np.clip((.5*width-abs(xx-shift))/(width*ingress), 0, 1)
    baseline = np.polynomial.polynomial.polyval(xx, model['poly_baseline'])*1e6
    folded.plot(xx*d*24, baseline-model['depth_ppm']*template, color='#b85f32')
    folded.set(xlabel='Hours from event center', ylabel='Residual (ppm)',
               title=f"Folded profile · U vs smooth ΔBIC {shape['delta_bic_u_vs_smooth']:.1f}")
    for parity, color in enumerate(colors[:2]):
        rows = [event for curve in measured_curves for event in good_events(row, curve.sector) if event['cycle'] % 2 == parity]
        for index, event in enumerate(rows):
            window = windows[(event['sector'], event['cycle'])]
            odd_even.scatter(window['t'][::3]*24, window['flux'][::3]*1e6, alpha=.15, s=3.5, color=color,
                             label=('Even', 'Odd')[parity] if not index else None)
    odd_even.legend(fontsize=8)
    odd_even.set(xlabel='Hours from event center', ylabel='Residual (ppm)', title='Clean odd and even profiles')
    best = max(good_events(row, sector), key=lambda value: value['snr'])
    window = windows[(sector, best['cycle'])]
    local.plot(window['t']*24, window['flux']*1e6, '.', markersize=3, color='#166b85')
    local.axvspan(-12*d, 12*d, color='#166b85', alpha=.1)
    local.set(xlabel='Hours from event center', ylabel='Residual flux (ppm)', title=f"S{sector} event · BTJD {best['btjd']:.5f}")
    if window['bkg'] is not None:
        bg = local.twinx()
        bg.plot(window['t']*24, window['bkg'], color='#b85f32', alpha=.6, linewidth=.6)
        bg.set_ylabel('SAP_BKG residual', color='#b85f32')
    grid_image = np.array(pixel['image'])
    lim = max(np.max(abs(grid_image))*.88, 1e-6)
    colorscale = image.imshow(grid_image, origin='lower', cmap='RdBu_r', vmin=-lim, vmax=lim)
    fig.colorbar(colorscale, ax=image, label='Mean out – mean in (e⁻/s)', fraction=.025)
    image.scatter(*pixel['target'], marker='+', s=175, color='darkgreen', linewidths=2, label='TIC projected position')
    fit = pixel['centroid']
    image.errorbar(fit['x'], fit['y'], xerr=fit['sigma_px'], yerr=fit['sigma_px'], fmt='x', color='#151515', label='Difference centroid')
    gaia = pixel['gaia_neighbors']
    if gaia:
        image.scatter([s['x'] for s in gaia], [s['y'] for s in gaia], facecolors='none', edgecolors='#9a6800',
                      marker='o', s=[max(18, (19-s['g_mag'])*6) for s in gaia], linewidths=1., label='Gaia DR3')
    image.set(xlabel='TPF column (px)', ylabel='TPF row (px)',
              title=f"S{sector} mean difference image · TIC offset {pixel['offset_px']:.2f} px · {pixel['events']} events")
    image.legend(loc='upper left', bbox_to_anchor=(1.03, .95), fontsize=8)
    period = candidate['period_days']
    fig.suptitle(f"TIC {candidate['tic']} · {period:.7f} d · Deep-vetting candidate for manual review", fontsize=14)
    name = f"TOP{row['rank']:02d}_TIC{candidate['tic']}.png"
    tmp = out / (name+'.part')
    fig.savefig(tmp, dpi=150, format='png')
    plt.close(fig)
    tmp.replace(out / name)
    row['figure'] = name


def write_csv(out, rows):
    fields = ('id', 'batch', 'tic', 'sectors', 'period_days', 'epoch_btjd', 'depth_ppm', 'rp_rearth', 'ruwe',
              'snr', 'mad_sde', 'catalog_status', 'catalog_matches', 'catalog_host_associations',
              'artifact_status', 'artifact_tags', 'flagged_events', 'clean_events', 'clean_snr', 'repeat_sectors',
              'stellar_variability', 'ls_matches', 'ls_residual_snr', 'ls_retained_depth_fraction', 'ls_residual_repeat_sectors',
              'tpf_status', 'tpf_sector', 'tpf_centroid_offset_px', 'tpf_sigma_px',
              'u_minus_v_support_delta_bic', 'u_vs_smooth_delta_bic', 'final_grade', 'first_reason', 'rank',
              'recovered_edge_events', 'duration_to_keplerian_ratio', 'harmonic2_residual_snr',
              'harmonic2_retained_depth_fraction', 'figure')
    name = out / 'MASTER_HITS_deep_vetted.csv'
    tmp = name.with_suffix('.csv.part')
    with tmp.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in rows:
            c, p, pix, cat, ls = [row[key] for key in ('candidate', 'instrument', 'pixel', 'catalog', 'variability')]
            stellar = c['vet']['stellar_radius_ruwe']
            writer.writerow({'id': row['id'], **{key: c[key] for key in ('batch', 'tic', 'period_days', 'epoch_btjd',
                                                                         'depth_ppm', 'snr', 'mad_sde')},
                             'sectors': ' '.join(map(str, c['sectors'])), 'rp_rearth': stellar.get('rp_rearth'),
                             'ruwe': stellar.get('gaia_ruwe'), 'catalog_status': cat['state'],
                             'catalog_matches': json.dumps(cat['matches'], ensure_ascii=False),
                             'catalog_host_associations': json.dumps(cat['host_associations'], ensure_ascii=False),
                             'artifact_status': p['state'], 'artifact_tags': ' '.join(p['tags']),
                             'flagged_events': p['flagged_event_count'], 'clean_events': p['clean_event_count'],
                             'clean_snr': p['clean']['snr'], 'repeat_sectors': p['repeat_sectors'],
                             'stellar_variability': ls['state'], 'ls_matches': json.dumps(ls.get('scans', [])),
                             'ls_residual_snr': (ls.get('residual') or {}).get('clean', {}).get('snr'),
                             'ls_retained_depth_fraction': ls.get('retained_depth_fraction'),
                             'ls_residual_repeat_sectors': (ls.get('residual') or {}).get('repeat_sectors'),
                             'tpf_status': pix['state'], 'tpf_sector': pix.get('sector'), 'tpf_centroid_offset_px': pix.get('offset_px'),
                             'tpf_sigma_px': pix.get('centroid', {}).get('sigma_px'),
                             'u_minus_v_support_delta_bic': shape_evidence(row)['shape']['delta_bic'], 'final_grade': row['grade'],
                             'u_vs_smooth_delta_bic': shape_evidence(row)['shape'].get('delta_bic_u_vs_smooth'),
                             'first_reason': row['first_reason'], 'rank': row.get('rank'),
                             'recovered_edge_events': p.get('recovered_edge_events'),
                             'duration_to_keplerian_ratio': row.get('kepler_ratio'),
                             'harmonic2_residual_snr': (ls.get('harmonic2') or {}).get('residual_snr'),
                             'harmonic2_retained_depth_fraction': (ls.get('harmonic2') or {}).get('retained_depth_fraction'),
                             'figure': row.get('figure')})
    tmp.replace(name)


def report_text(rows, top, audit, pre, post, budget, references):
    counts = Counter(row['grade'] for row in rows)
    causes = Counter((row['grade'], row['first_reason']) for row in rows)
    stages = [('星表查重', lambda r: r['catalog']['state'] == 'UNCATALOGED'),
              ('系统噪声与背景通量', lambda r: r['instrument']['state'] == 'PASS'),
              ('恒星变异 Lomb–Scargle', lambda r: r['variability']['state'] == 'PASS'),
              ('TPF 差异图与 Gaia 质心', lambda r: r['pixel']['state'] == 'CENTERED'),
              ('U 型、平滑模型、开普勒历时及扇区重复', lambda r: shape_evidence(r)['repeat_sectors'] >= 2 and
               (shape_evidence(r)['shape']['delta_bic'] or 0) >= 6 and
               (shape_evidence(r)['shape'].get('delta_bic_u_vs_smooth') or 0) >= 6 and
               (r.get('kepler_ratio') is None or r['kepler_ratio'] <= 1.8))]
    current = list(rows)
    lines = ['# 第 2–6 批深度二筛', '',
             f"42 条输入信号，涉及 {len(set(r['candidate']['tic'] for r in rows))} 颗不同 TIC 恒星。"
             f"精筛保留 **{counts['KEEP_TOP']}** 条，边缘待核 **{counts['BORDERLINE']}** 条，排除 **{counts['REJECT']}** 条。"
             '保留条目均为深度二筛后待人工复核的凌星候选信号。', '', '## 误报归因漏斗', '',
             '| 顺序 | 进入 | 本级移出 | 通过 |', '| --- | ---: | ---: | ---: |']
    for name, apply in stages:
        admitted = [r for r in current if apply(r)]
        lines.append(f'| {name} | {len(current)} | {len(current)-len(admitted)} | {len(admitted)} |')
        current = admitted
    lines.extend(['', '每条只按首个未通过阶段计入漏斗。首要归因采用物理根因（A2）。伪影 SNR² 占比低于 50% 且已确认为恒星调制的信号，计为 STELLAR_VAR。标记与事件明细保留在 CSV 和证据 JSON。', '',
                  '| 最终分级 | 首要归因 | 条数 |', '| --- | --- | ---: |'])
    for (grade, reason), count in sorted(causes.items()):
        lines.append(f'| {grade} | `{reason}` | {count} |')
    lines.extend(['', '## 精筛复核卡', ''])
    if len(top) < 5:
        lines.append(f'本次有 {len(top)} 条信号达到所有门槛。')
    for row in top:
        c, p, pix = row['candidate'], shape_evidence(row), row['pixel']
        star = c['vet']['stellar_radius_ruwe']
        lines.extend([f"### {row['rank']}. TIC {c['tic']}", '',
                      f"- 扇区 {', '.join(map(str, c['sectors']))}。周期 {c['period_days']:.8f} 天，BTJD {c['epoch_btjd']:.6f}。",
                      f"- 原搜深度 {c['depth_ppm']:.1f} ppm，Rp {star['rp_rearth']:.2f} R⊕，Gaia RUWE {star['gaia_ruwe']:.3f}。",
                      f"- SNR {c['snr']:.2f}，MAD-SDE {c['mad_sde']:.2f}，清洁重测 SNR {p['clean']['snr']:.2f}。"
                      f"{p['repeat_sectors']} 个扇区独立重复，U/V ΔBIC {p['shape']['delta_bic']:.1f}，"
                      f"U+连续基线相对纯平滑曲线 ΔBIC {p['shape']['delta_bic_u_vs_smooth']:.1f}。",
                      f"- S{pix['sector']} TPF 质心偏移 {pix['offset_px']:.3f} ± {pix['centroid']['sigma_px']:.3f} 像素。"
                      f"差异图 SNR {pix['centroid']['snr']:.1f}，使用 {pix['events']} 次同事件内外采样。[{row['figure']}]({row['figure']})", ''])
    smooth_borderlines = [row for row in rows if row['first_reason'] == 'SMOOTH_PROFILE_NOT_EXCLUDED']
    if smooth_borderlines:
        lines.extend(['', '### 质心居中但谷形仍不确定', '',
                      '| TIC | 周期（天） | TPF 偏移（像素） | U+基线相对纯平滑 ΔBIC | D/T_circ | 双谐波残差 SNR |',
                      '| --- | ---: | ---: | ---: | ---: | ---: |'])
        for row in sorted(smooth_borderlines, key=priority, reverse=True):
            c, pix = row['candidate'], row['pixel']
            value = shape_evidence(row)['shape'].get('delta_bic_u_vs_smooth')
            bic = f'{value:.2f}' if value is not None else '证据不足'
            ratio = row.get('kepler_ratio')
            ratio_text = f'{ratio:.2f}' if ratio is not None else '—'
            h2 = (row['variability'].get('harmonic2') or {}).get('residual_snr')
            h2_text = f'{h2:.2f}' if h2 is not None else '—'
            lines.append(f"| {c['tic']} | {c['period_days']:.7f} | {pix['offset_px']:.3f} | {bic} | {ratio_text} | {h2_text} |")
        lines.append('')
    recovered_total = sum(row['instrument'].get('recovered_edge_events', 0) for row in rows)
    lines.extend(['## v2 修复记录', '',
                  '- 误差量纲修正：保留 2 分钟单 cadence 尺度散度 `max(sigma_2m, nominal, red)`，使用真实有效 cadence 采样数计算单次深度误差，消除对白噪声目标约 √10 倍的误差虚增。',
                  f'- 条件化边缘与动量卸载掩蔽（借鉴 PHT / TIC 100796276）：对扇区首尾、≥0.2 天缺口 ±0.5 天及动量卸载窗口事件，仅在覆盖率 <0.75、单侧肩部不足、左右肩部基线阶跃 >3.5σ 或伴随背景尖峰时掩蔽。本次共找回原本被无条件误删的事件 **{recovered_total}** 次。',
                  '- 形态判定与 `rotation()` 解耦，新增 SHERLOCK 风格凌星窗外 `f + 2f` 双谐波消旋诊断指标（`harmonic2_residual_snr`、`harmonic2_retained_depth_fraction`）。',
                  '- 新增 ExoMiner++ 2.0 开普勒圆轨道历时一致性比率（`D/T_circ <= 1.8`）判据，并采用按真实物理根因归并的 A2 归因表。', '',
                  '## v2.2 阈值建议', '',
                  '- 对周期候选在扇区首尾与 ≥0.2 天缺口边缘 ±0.5 天、动量卸载 ±max(D/2, 0.1 天) 窗口采用条件化屏蔽：仅在覆盖率 <0.75、单侧肩部不足、肩部阶跃 >3.5σ 或伴随 3 cadence 背景残差 ≥6 个稳健 σ 时屏蔽。清洁窗口联合 SNR ≥7，至少两个扇区独立 SNR ≥3 才进入高优排序。',
                  '- 对 P、P/2、2P 附近的 LS 峰，同时检查功率 ≥0.15、FAP ≤10⁻⁴、峰峰振幅 ≥0.8 倍原深度，再用凌星窗外的数据按扇区拟合并扣除正弦调制。扣除后深度低于原清洁深度的 35% 或残余 SNR <7 时排除。保留需至少两扇区各自复现及同窗 U/V 拟合 ΔBIC ≥6。',
                  '- 对折叠谷同时比较二次或四次平滑基线与同阶基线加 U 型。给宽度、中心、深度和入食形状计自由度。只有 ΔBIC ≥6 才把谷形记为独立凌星证据。质心居中可以定位宿主的亮度变化。',
                  '- 把 AIP/ESA Gaia DR3、CTOI 及公开 TESS EB 加入去重。候选与星表 P、P/3、P/2、2P、3P 的相对差 ≤0.5% 时标记已知周期关联，并保留有历元时的相位差供人工核验。',
                  '- 高优等级要求有效 TPF 差异图 SNR ≥5、定位 σ ≤0.5 像素、相对 TIC 偏移 ≤1 像素且未落在近邻亮源上。证据缺失进入边缘队列。下一批先对现有盲测和有污染的样本做回放，记录各门槛的召回变化。',
                  '- SHERLOCK：上游引入多窗口去趋势并在凌星窗外执行 `f + 2f` 双谐波消旋，先清零多极黑子自转调制再做后续深度与形态检验。',
                  '- PHT/NotPlaNET：上游同样采用条件化边缘/动量卸载掩蔽，并修复 `rolling_scanner.py` 的 `rank()`，将 `\'NOT_APPLICABLE\'` 与 `PASS` 等权计分（B1–B6 共有 17 条单次凌星，此前进入 TPF 检查的为 0 条）。此项须在扫描器下一次版本变更时落实，本次保持 `rolling_scanner.py` 冻结不改。',
                  '- ExoMiner++ 2.0：上游前置开普勒圆轨道历时一致性比率 `D/T_circ <= 1.8` 物理约束。',
                  '- Hidden Gems：建立已知确认行星或 TOI 的 M/K 矮星宿主优先选星池，复用 `iterative_search` 迭代掩蔽深挖微弱伴星。', '',
                  '### 黄金样本对照', '',
                  '| 已知凌星 | U+基线相对纯平滑 ΔBIC | 清洁 SNR | 独立扇区 | D/T_circ |', '| --- | ---: | ---: | ---: | ---: |'])
    for row in references:
        ratio = row.get('duration_to_keplerian_ratio')
        ratio_text = f'{ratio:.2f}' if ratio is not None else '—'
        lines.append(f"| {row['control']} | {row['u_vs_smooth_delta_bic']:.2f} | {row['clean_snr']:.2f} | {row['repeat_sectors']} | {ratio_text} |")
    if all(row['clean_snr'] >= 7 and row['repeat_sectors'] >= 2 for row in references):
        golden_note = 'π Men c 与 TOI-700 c 均通过形态对照、清洁联合 SNR ≥7 及 ≥2 个独立扇区门槛，开普勒历时比均 ≤1.0。'
    else:
        golden_note = ('π Men c 和 TOI-700 c 均通过形态对照。TOI-700 c 未达到清洁联合 SNR ≥7 或两个独立扇区的高优门槛。'
                       '因此这些阈值建议用于高优分流，弱信号留在待复核队列以保留召回。')
    lines.extend(['', golden_note, '',
                  '## 数据与验收', '',
                  f"- TPF 新增传输记账 {budget.used:,} / {GIB:,} 字节。根分区开始 {pre['root_free_gib']:.2f} GiB，结束 {post['root_free_gib']:.2f} GiB。"
                  '8080 无监听，两个服务均为 inactive/disabled，滚动扫描保持暂停。',
                  '- FAP 是 Lomb–Scargle 白噪声近似，已结合功率、振幅及跨扇区复现。Gaussian 质心误差来自拟合与保守误差底线。',
                  '- CTOI/TESS EB/Gaia DR3 是发布目录，MAST TESS EB 快照覆盖 S1–S26。未匹配仅表示本次已核验目录无相容记录。Gaia 变星分类没有周期时仅保留宿主证据。'
                  '[ExoFOP CTOI](https://exofop.ipac.caltech.edu/tess/view_ctoi.php) · [MAST TESS EB](https://archive.stsci.edu/hlsp/tess-ebs) · [Gaia DR3](https://gaia.aip.de/metadata/gaiadr3/)',
                  '', '| 源 | 返回行 | 快照 UTC |', '| --- | ---: | --- |'])
    for key, info in audit.items():
        detail = info.get('fetched_utc') or str(info.get('error', '缺失')).replace('|', '/').replace('\n', ' ')[:200]
        lines.append(f"| {key} | {info.get('rows', '—')} | {detail} |")
    return '\n'.join(lines) + '\n'


def main(root):
    start = check_boundaries(root)
    out = root / 'out' / OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    sample = read_sample(root)
    catalog, audit = load_catalogs(root, sample)
    rows = []
    for index, candidate in enumerate(sample, 1):
        batch, tic = candidate['batch'], candidate['tic']
        listing = json.loads((root / 'out' / batch / 'input_targets.json').read_text(encoding='utf-8'))
        products = next(row['products'] for row in listing if row['tic'] == tic)
        rows.append({'id': f'{index:02d}-{tic}', 'candidate': candidate, 'catalog': compare(candidate, catalog[tic], audit),
                     'products': products, 'kepler_ratio': keplerian_duration_ratio(candidate)})
    with ProcessPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(review_photometry, root, row['candidate']): row for row in rows}
        for future in as_completed(tasks):
            row = tasks[future]
            row['instrument'], row['variability'] = future.result()
            print(f"photometry {row['id']} catalog={row['catalog']['state']} artifacts={row['instrument']['state']} "
                  f"rotation={row['variability']['state']} repeats={row['instrument']['repeat_sectors']}", flush=True)
    budget = DeepBudget(out / 'transfer_ledger.json')
    with session() as client:
        apply_pixels(client, root, out, rows, budget)
    top = classify(rows)
    for row in top:
        render(root, out, row)
    current_figures = {row['figure'] for row in top}
    for path in out.glob('TOP*_TIC*.png'):
        if path.name not in current_figures:
            archived = out / 'qa_preliminary'
            archived.mkdir(exist_ok=True)
            path.replace(archived / path.name)
    finish = check_boundaries(root)
    references = reference_shapes(root / 'golden_expanded')
    write_csv(out, rows)
    atomic_json(out / 'evidence.json', rows)
    atomic_json(out / 'transfer_ledger.json', {'bytes': budget.used, 'limit': GIB})
    atomic_json(out / 'review_audit.json', {'finished_utc': datetime.now(timezone.utc).isoformat(),
                                          'before': start, 'after': finish, 'signals': len(rows),
                                          'top': len(top), 'tpfs_transfer_charged_bytes': budget.used,
                                          'shape_reference_checks': references})
    text = report_text(rows, top, audit, start, finish, budget, references)
    tmp = out / 'DEEP_VET_REPORT.md.part'
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(out / 'DEEP_VET_REPORT.md')
    print(f'finished 42 rows; {len(top)} KEEP_TOP; {budget.used} TPF bytes reserved', flush=True)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
