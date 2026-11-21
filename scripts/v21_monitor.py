"""Monitor the Jetson scanner and persist noteworthy status changes for the Windows receiver."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from v21_sync import status


INTERVAL = 30
STALL_AFTER = 15 * 60


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def completion(root, state, plan):
    identifiers = set()
    recovered = high = 0
    for number in range(plan['first_batch'], plan['last_batch'] + 1):
        batch = root / 'out' / f'v21_batch_{number:05d}' / 'batch_status.json'
        row = read_json(batch, {})
        ids = row.get('science_ids', [])
        controls = row.get('blind', [])
        if (row.get('searched') != 102 or row.get('science') != 100 or len(set(ids)) != 100 or
                len(controls) != 2 or not row.get('blind_pass') or not all(
                    control.get('recovered') and isinstance(control.get('period_error_pct'), (int, float))
                    and 0 <= control['period_error_pct'] <= .5 for control in controls
                ) or identifiers.intersection(ids)):
            raise ValueError(f'第 {number} 批的报告、目标数或盲测结果未通过核验')
        identifiers.update(ids)
        recovered += len(controls)
        high += row['high_priority']
    expected = plan['science_target_count']
    if len(identifiers) != expected or not identifiers.issubset(state['scanned_tic_ids']):
        raise ValueError('批次报告与扫描提交状态不一致')
    return {'science': expected, 'blind_recovered': recovered, 'high_priority': high}


def last_activity(root, batch):
    paths = [root / 'logs/rolling_v21.log', root / 'state/scanner_state.json', root / 'cache/rolling_v21']
    if isinstance(batch, int):
        name = f'v21_batch_{batch:05d}'
        output = root / 'out' / name
        raw = root / 'raw' / name
        paths.extend((output, output / 'checkpoints', raw))
        paths.extend(raw.glob('*.part'))
    times = []
    for path in paths:
        try:
            times.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return max(times, default=None)


def assess(root, live, state, now):
    plan = state.get('increment') or {}
    snapshot = {**live, 'increment': plan or None, 'verified_result': None, 'minutes_without_progress': None}
    if live['rolling_active']:
        latest = last_activity(root, live['next_batch'])
        minutes = max(0, int((now - latest) // 60)) if latest is not None else None
        snapshot['minutes_without_progress'] = minutes
        stalled = minutes is not None and minutes * 60 >= STALL_AFTER
        snapshot['health'] = 'no_progress' if stalled else 'running'
        if stalled:
            snapshot['title'] = 'TESS 扫描长时间无进展'
            snapshot['message'] = f"第 {live['next_batch']} 批 · 已有 {live['pending_batch_checkpoints']} / 102 个检查点，{minutes} 分钟无可见进展。"
        return snapshot
    if plan.get('outcome') == 'complete' and live['next_batch'] == plan.get('last_batch', -1) + 1:
        try:
            verified = completion(root, state, plan)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            snapshot.update(health='unverified', title='TESS 完成结果需要核对', message=str(exc))
        else:
            snapshot.update(health='complete', verified_result=verified, title='TESS 增量扫描完成',
                            message=f"已核验 {verified['science']} 颗科学目标，{verified['blind_recovered']} 次盲测通过，"
                                    f"{verified['high_priority']} 条高优待复核信号。结果已同步。")
        return snapshot
    reason = live['stop_reason']
    if reason or plan.get('outcome') in ('running', 'paused'):
        summary = reason.splitlines()[-1] if reason else plan.get('reason', '扫描已退出，等待核对日志。')
        snapshot.update(health='paused', title='TESS 扫描已暂停', message=summary[:320])
        return snapshot
    return {**snapshot, 'health': 'idle'}


def update(root, destination):
    previous = read_json(destination, {})
    state = read_json(root / 'state/scanner_state.json', {})
    snapshot = assess(root, status(root), state, time.time())
    events = previous.get('events', [])
    health = snapshot['health']
    alert = None
    if health in ('complete', 'unverified', 'no_progress', 'paused') and (
            health != previous.get('health') or snapshot.get('stop_reason') != previous.get('stop_reason')):
        alert = snapshot['title'], snapshot['message']
    elif health == 'running' and previous.get('health') == 'no_progress':
        alert = 'TESS 扫描恢复进展', f"正在处理第 {snapshot['next_batch']} 批。"
    elif (health == 'running' and previous.get('last_completed_batch') is not None
          and snapshot['last_completed_batch'] != previous['last_completed_batch']):
        plan = snapshot['increment'] or {}
        count = snapshot['science_targets_committed']
        if plan.get('outcome') == 'running':
            committed = (snapshot['next_batch'] - plan['first_batch']) * 100
            message = f"本轮已提交 {committed} / {plan['science_target_count']} 颗科学目标。正在处理第 {snapshot['next_batch']} 批。"
        else:
            message = f"累计提交 {count} 颗科学目标。正在处理第 {snapshot['next_batch']} 批。"
        alert = 'TESS 批次完成', message
    if alert:
        events.append({'id': uuid4().hex, 'kind': health, 'title': alert[0], 'message': alert[1],
                       'utc': datetime.now(timezone.utc).isoformat()})
        print(f'{events[-1]["utc"]} {alert[0]} · {alert[1]}', flush=True)
    snapshot['events'] = events[-64:]
    temp = destination.with_suffix('.json.part')
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    destination = args.root / 'state/v21_monitor.json'
    while True:
        try:
            update(args.root, destination)
        except (OSError, ValueError, KeyError) as exc:
            print(f'Monitor read will retry: {exc}', flush=True)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
