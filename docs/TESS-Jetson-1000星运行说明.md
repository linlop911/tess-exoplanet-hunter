# TESS Jetson 1000 星自动扫描

Jetson 从 MAST 的批量产品索引选出具备两扇区 SPOC 120 秒光变的目标，并用 TIC 恒星属性筛选。固定 1000 个不同 TIC，分成 10 批，每批 100 颗。首批含两颗独立的已知行星对照，排除黄金样本。光变下载、去趋势、周期和单次事件搜索、星历匹配、像素抽查和出图均由 Jetson 执行。

## 回归与筛查

- 周期搜索将 BLS 输入按观测段估计噪声并进行六倍尺度限幅，以降低极端点对周期谱的影响。保留实际光变用于单次事件检测和诊断。`SNR ≥7.5`、`SDE ≥6.5` 门槛沿用。多扇区复测中，π Men c 的 SDE 为 12.01，TOI-700 c 为 24.59，单次注入和缺口边缘检查通过。生产启动时匹配黄金报告内的搜索代码 SHA-256。
- 科学目标按 TIC 污染比和星等排序，限定 `Tmag < 11.5`、`2500–6200 K` 和 `log g ≥4.0`。每颗星处理后记录实测小时尺度 MAD。两扇区的观测窗限制长周期事件的覆盖，单次触发只记录 BTJD 时刻、深度和持续时间。
- 一颗星最多保留 5 条去重的单次候选，并完整记录原始触发数。每批先对优先候选中的 15 条做像素抽查，再更新排序并输出最终前 15 条诊断图。像素证据不足时记为 `INCONCLUSIVE`。批次结束后读盲测答案，保留解盲前排名的 SHA-256。盲测漏检时记录诊断并提醒先排查再研判。

## 运行与结果

- Jetson 工作目录为 `/home/data/astro_tess/`。每批下载限额为 3 GiB，光变和像素产品共用一个持久账本。每颗星写入原子检查点，重启沿用已校验产品和已完成检查点。每批启动时检查根分区、NVMe、8080 和两个指定服务。
- Jetson 使用四个 CPU 工作进程。`campaign.log` 记录批次状态，`out/campaign_progress.json` 在各批完成时更新。Windows 后台同步器每 60 秒通过 USB SSH 拉取已完成批次的 CSV、JSON、PNG 和摘要。USB 暂时断开时同步器会重试，Jetson 独立运行。
- Windows 输出在 `D:\SPARK\astro_out\batch_001` 至 `batch_010`。`batch_summary.md` 记录目标数、失败项、候选漏斗和盲测，`candidates_ranked.csv` 用于快速查看，JSON 保留质量检查细节。星表未匹配的信号仍需人工核验。

```powershell
py -3.11 D:/SPARK/astro_tess/jetson_job.py status
py -3.11 D:/SPARK/astro_tess/jetson_job.py retrieve
```

任务退出后可用 `py -3.11 D:/SPARK/astro_tess/jetson_job.py start` 从同一目标清单和检查点续跑。

[MAST 批量产品索引](https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_ffi-tp-lc-dv.html) · [扩展黄金回归报告](../astro_out/golden_expanded/golden_report.json)
