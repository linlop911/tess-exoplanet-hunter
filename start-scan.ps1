param(
    [int]$Batches = 5,
    [ValidateSet('hybrid', 'hidden_gems', 'standard')]
    [string]$Pool = 'hybrid',
    [int]$Workers = 4,
    [switch]$StopOnHit,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$ScriptsDir = Join-Path $ProjectDir 'scripts'

if ($Stop) {
    Write-Host '[TESS] 正在写入 Jetson STOP 哨兵（当前批次收尾后安全停机）...' -ForegroundColor Yellow
    & py -3.11 (Join-Path $ScriptsDir 'jetson_job.py') command "touch /home/data/astro_tess/STOP && ls -l /home/data/astro_tess/STOP"
    Write-Host '[TESS] STOP 已生效。' -ForegroundColor Green
    exit 0
}

if ($Status) {
    Write-Host '[TESS] 正在查询 Jetson 运行状态与安全边界...' -ForegroundColor Cyan
    & py -3.11 (Join-Path $ScriptsDir 'jetson_job.py') command "echo '=== PROCESS ==='; ps -ef | grep '[r]olling_scanner.py' || echo STOPPED; echo '=== STOP ==='; ls -l /home/data/astro_tess/STOP 2>/dev/null || echo NO_STOP; echo '=== DISK ==='; df -BG / /home/data; echo '=== STATE ==='; head -n 5 /home/data/astro_tess/state/scanner_state.json; echo '=== LOG ==='; tail -n 8 /home/data/astro_tess/logs/rolling_v21.log"
    exit 0
}

if ($Batches -lt 1 -or $Batches -gt 50) {
    throw "Batches 必须在 1 到 50 之间（当前: $Batches）"
}

$Mode = if ($StopOnHit) { 'stop_on_hit' } else { 'continuous' }
Write-Host "[TESS] 准备启动独立扫描：新增 $Batches 批 | 选星池=$Pool | 模式=$Mode | 并行=$Workers 核" -ForegroundColor Cyan

$pyCode = @"
import json, sys
sys.path.insert(0, r'$ScriptsDir')
import jetson_job

with jetson_job.connect() as c:
    # 1. 安全边界预检
    check = jetson_job.run(c, "df -BG --output=avail / /home/data | tail -n 2; ss -tln | grep ':8080 ' | wc -l; if ! flock -n /home/data/astro_tess/logs/rolling_v21.lock true; then echo BUSY; else echo IDLE; fi")
    lines = [x.strip() for x in check.strip().splitlines() if x.strip()]
    root_avail = int(lines[0].rstrip('G'))
    nvme_avail = int(lines[1].rstrip('G'))
    port8080 = int(lines[2])
    lock_state = lines[3]
    print(f'[预检] 根分区剩余={root_avail}GiB | NVMe剩余={nvme_avail}GiB | 8080监听={port8080} | 锁状态={lock_state}')
    if lock_state == 'BUSY':
        raise SystemExit('已有扫描任务正在运行，取消重复启动。')
    if root_avail < 25 or nvme_avail < 100:
        raise SystemExit(f'磁盘剩余不足（要求 / >=25GiB 且 /home/data >=100GiB），已中止启动。')
    if port8080 > 0:
        raise SystemExit('检测到 Jetson 8080 端口（大模型服务）正在运行，为避免抢占资源已中止启动。')

    # 2. 自动读取下一批次号
    st = json.loads(jetson_job.run(c, 'cat /home/data/astro_tess/state/scanner_state.json'))
    first_batch = int(st['batch'])
    last_batch = first_batch + $Batches - 1
    print(f'[计划] 将自动从 Batch {first_batch} 跑到 Batch {last_batch}（共 {$Batches} 批 = {$Batches * 100} 颗科学目标）')

    # 3. 解除 STOP + 确认监控在线 + 按 v2.2 指标启动
    jetson_job.run(c, 'rm -f /home/data/astro_tess/STOP')
    jetson_job.ensure_monitor(c)
    inv = (f'{jetson_job.PYTHON} {jetson_job.ROOT}/scripts/rolling_scanner.py '
           f'--root {jetson_job.ROOT} --mode $Mode --last-batch {last_batch} '
           f'--target-pool $Pool --workers $Workers')
    pid = jetson_job.launch(c, 'rolling_v21',
                            ('tess_vetting.py', 'tess_v21.py', 'rolling_sources.py', 'rolling_scanner.py'),
                            inv).strip()
    print(f'[已启动] Jetson PID={pid} | 模式=$Mode | 选星池=$Pool | 终点=Batch {last_batch}')
"@

& py -3.11 -c $pyCode
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 4. 确保 Windows 同步任务运行并打开本地看板
Start-ScheduledTask -TaskName 'SPARK-TESS-V21-ReportSync' -ErrorAction SilentlyContinue
& powershell -ExecutionPolicy Bypass -File (Join-Path $ProjectDir 'start-dashboard.ps1')
