# TESS V2.1 Jetson 滚动扫描

Jetson 使用 `/home/data/miniforge3/envs/astro` 独立运行。滚动调度、MAST 与星表拉取、回归、四个 CPU 工作进程、候选筛查、报告和清理均在 Jetson。Windows 负责部署和接收报告。

## 首次试跑

- 启动参数为 `--mode stop_on_hit --max-batches 5 --workers 4`，每批 100 颗科学目标加 2 颗盲测。触发高优信号、任一熔断条件或 5 批试跑上限时退出。连续模式需明确切换。
- 命中暂停后，`v21-start` 从扫描状态中的下一批恢复，并按累计 5 批计算剩余试跑额度。
- 限量连续扫描由 `v21-increment` 接管。它等当前进程提交批次后，用同一检查点连续跑完指定的 5 批。高优信号仍生成复核卡。`scanner_state.json` 的 `increment` 记录起止批次和执行状态。原有熔断条件继续生效。
- MAST 公共索引自动探测最新常规扇区。科学目标优先选最新扇区和较早的同半球扇区，取三份 120 秒光变并要求目录标注跨度至少 731 天。TIC 限定 `Tmag < 11.5`、`R* ≤ 1.2 R☉`、`log g ≥4` 和距离不超过 300 pc。Gaia DR2 经 Gaia DR3 邻域表关联，歧义关联保留待核验。
- FITS 首选 MAST 的 AWS 公共镜像，支持 HTTP Range 续传。并发下载上限为 2，单连接限速约 6 MiB/s。每批光变和像素合用 3 GiB 持久预算。优先候选最多 15 条进入 TPF 像素补拉。传输记录保留 SHA-256、产品来源和续传字节位置。
- 周期搜索范围为 0.8–45 天。通过 `SNR ≥7.5 且 MAD-SDE ≥6.5` 或当轮第一峰 `SNR ≥15` 进入下一轮掩蔽，最多 3 轮。单次检测检查 2、4、8 小时窗口，要求深度大于 5.5 倍局部稳健散度，并检查耀斑和缺口。
- 高优候选须通过全部适用的七项筛查。Gaia、TPF 或观察覆盖不足保留 `INCONCLUSIVE`。奇偶、次食和倍周期检查对单次事件标为 `NOT_APPLICABLE`。每条候选记录保留 TPF、SAP/PDCSAP、星表与食双星证据。

## 运行状态与停止

- 活动日志在 `/home/data/astro_tess/logs/rolling_v21.log`，进度与已扫 TIC 在 `state/scanner_state.json`。每颗星独立保存原子检查点，网络重试沿用同一冻结批次和已校验文件。连续三次批次拉取或校验失败写入 `STOP_REASON.txt` 并退出。
- `STOP` 哨兵在批次边界执行。根分区少于 25 GiB、NVMe 少于 100 GiB、Swap 超过 1024 MiB 或温度持续 60 秒高于 80 °C 时，Jetson 监督进程结束工作进程组并保留已提交状态。批次盲测漏检或周期误差超过 0.5% 时停止扩批，保留该批数据供定位。
- 批次报告和盲测通过后，清理仅限当前批次清单内未留存的 FITS。高优候选和盲测目标的 FITS 保留。`MASTER_HITS.csv`、`MASTER_HITS.md` 和命中卡 `HIT_ALERT_v21_batch_*.md` 由 Jetson 写入 `out/`。

```powershell
py -3.11 D:/SPARK/astro_tess/jetson_job.py v21-status
py -3.11 D:/SPARK/astro_tess/jetson_job.py v21-export
py -3.11 D:/SPARK/astro_tess/jetson_job.py command 'touch /home/data/astro_tess/STOP'
```

Windows 接收目录为 `D:\SPARK\astro_out\v21`。Windows 登录时启动 `SPARK-TESS-V21-ReportSync`，接收程序断线重连。Jetson 每 30 秒通过现有 SSH 会话发送进度、变动的报告和诊断图。可在 `sync_status.json` 检查最近更新时间，接收日志位于 `D:\SPARK\logs\tess-v21-sync.log`。`v21-export` 仍可手动取回文件。SSH 断开时 Jetson 的当前任务继续运行。下次启动扫描前应确认是否保留 `STOP` 哨兵。

Jetson 的独立监测进程每 30 秒核对扫描锁、日志、下载和检查点。各批提交、任务完成、暂停或连续 15 分钟无可见进展时记录事件。增量完成前逐批核对报告、唯一目标数及盲测周期误差。监测事件经现有 SSH 同步，由 Windows 接收器提交桌面通知。Windows 离线时，Jetson 继续记录事件，重连后接收端补发。下次启动扫描时一并检查监测进程。监测结果在 [v21_monitor.json](../astro_out/v21/v21_monitor.json)。

## 黄金样本与独立复核

V2.1 复用已通过校验的 π Men 五扇区和 TOI-700 十一扇区 AWS 光变。实跑约 90 秒，π Men c 和 TOI-700 c、b、d 均恢复。4 小时合成事件通过，缺口边缘零触发。候选卡记录 TIC、扇区、BTJD、周期或单次事件、深度、估算半径、RUWE、像素质心结论与四联图。星表使用 PS、TOI 和 MAST TESS-EB 发布目录。[V2.1 黄金报告](../astro_out/v21/v21_golden/golden_report.json) · [MAST TESS-EB](https://archive.stsci.edu/hlsp/tess-ebs)

Jetson 实测从 AWS 续传 64 KiB 处的 FITS 分片，合并后的 SHA-256 与完整黄金样本一致。发现个别已发布 FITS 内部 HDU 的 `CHECKSUM` 不匹配，此时仅在 AWS 对象 ETag 的单段 MD5、文件结构、TIC 和扇区均核验通过时接受，并记录不匹配的 HDU。Gaia、TESS-EB 和清理路径也实测通过。各熔断条件通过模拟验证。[验收日志](../astro_out/v21/v21_smoke.log)

有命中时，先审计盲测、BTJD 时间基准、噪声窗口和 `2P` 结果。独立周期复核可向 SHERLOCK 提供同一 TIC 和扇区，对照周期、历元、深度和谐波。ExoMiner++ 需先确认适配的 TESS DV 数据可用。ExoFOP 人工核对 TOI、社区候选、目标注释与近邻食双星。未编目信号统一称为待复核凌星候选信号。[SHERLOCK](https://github.com/franpoz/SHERLOCK) · [ExoMiner](https://github.com/nasa/Exominer) · [ExoFOP TESS](https://exofop.ipac.caltech.edu/tess/)
