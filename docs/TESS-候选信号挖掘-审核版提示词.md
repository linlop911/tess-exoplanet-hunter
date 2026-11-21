# TESS 候选信号挖掘 · 审核版提示词

在 Windows `D:\SPARK` 获取公开数据，在 Jetson AGX Orin 上用确定性 Python 处理 TESS SPOC 120 秒光变。分别搜索弱周期凌星和孤立凌星事件。按预检、黄金样本、首批扫描、复核的顺序执行，每阶段保存输入清单、参数和验收结果。

## 预检与执行边界

- 从 Windows 实测 MAST 产品目录和下载，可使用 [MAST 的 AWS 公开镜像](https://registry.opendata.aws/mast-tess/)。各下载 π Men 与 TOI-700 的一个 SPOC `_lc.fits` 样本。建立样本清单，记录 TIC、扇区、曝光时间、产品 URI、版本、字节数和 SHA-256。用 FITS 解析器核对 `TICID`、`SECTOR`、时间参考、时间尺度和 `TIME`、`QUALITY`、`PDCSAP_FLUX`、`PDCSAP_FLUX_ERR` 列。另对一个对应的 SPOC `_tp.fits` 做小样本验证，供质心检查使用。
- 用官方 [NASA Exoplanet Archive TAP](https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html) 实查 `ps` 和 `toi`。`ps.tic_id` 是带 `TIC ` 前缀的字符串，`toi.tid` 是目标编号。保存查询文本、抓取时间和响应。Windows 若经默认代理连接失败，可试进程级直连并保留 TLS 校验。记录元数据请求、重定向和最终文件读取各自的结果。
- 用普通账号经 USB SSH 对 Jetson 做只读检查，核验 aarch64、`/home/data` 独立挂载且可写、根分区可用空间至少 25 GiB、8080 无监听、`zerotier-one.service` 与 `report_robot_network_info.service` 均为 `inactive/disabled`。任一条件不满足就记录现状并暂停。禁止改系统 Python、JetPack、CUDA、`/etc/fstab`、systemd 配置或现有模型环境。
- 预检通过后使用 `/home/data/miniforge3` 下独立的 `astro` 环境和 `/home/data/astro_tess/{scripts,golden,raw,out}`。将包缓存、临时文件、绘图缓存与日志定向到 NVMe。记录环境锁定清单。首批下载上限为 3 GiB，包含光变和像素产品。先按产品大小编制清单，缺少可靠大小时用实际写入字节约束，重试与续传沿用同一账本。预留像素复核配额，仅为触发候选按需拉取关键扇区的像素文件。

## Stage 0 · 黄金样本回归

- Windows 按预先冻结的扇区清单下载并校验文件，经 USB 同步到 Jetson `golden/`。使用当前默认 `ps` 星历，π Men c（TIC 261136679）参考周期约 6.267823 天，TOI-700（TIC 150428135）以 b、c、e 为默认周期验收集合。TOI-700 主任务有 11 个扇区的 120 秒数据（1、3–11、13），优先用这个范围建立多扇区基准。TOI-700 d 约 37.423457 天，周期超出首批 0.8–30 天搜索范围，可作为单次通道的星历对照。[TOI-700 验证论文](https://ntrs.nasa.gov/citations/20210012798)
- 根据 FITS 元数据将搜索和输出统一为 BTJD（`BJD_TDB − 2457000`）。核对星表参考历元的时间尺度后再比对相位。当前黄金样本的默认 `ps` 历元只标为 `BJD`，记录这一限制，黄金回归以半个观测凌星持续时间作为历元匹配容差。逐扇区应用 `QUALITY == 0`、非有限值清理、仅向上的异常点裁剪及保凌星去趋势。按真实间隙分段，保存处理前后点数和有效观测覆盖。
- `golden_calibration.py` 调用后续生产搜索函数，以同一阈值和预声明的候选排名范围进行盲搜。要求 π Men c 和 TOI-700 的 b、c、e 中至少一颗周期误差 ≤0.5%，且观测覆盖下的事件时刻与星历相符。输出两张折叠 PNG 和 `golden_report.json`。先核实数据覆盖与信号可检性，再诊断漏检。禁止用已知周期限制 BLS 搜索范围。
- 为单次通道增加独立回归。选取有真实缺口和噪声的光变，在去趋势前注入已记录的孤立凌星，检查时刻恢复、缺口边缘抑制及未注入窗口的触发情况。预先记录注入参数、匹配公差和候选阈值，分别报告两条通道的结果。阶段未通过时暂停未知目标扫描。

## Stage 1–3 · 首批扫描与候选筛查

- `select_targets.py` 取 98 颗科学目标及 2 颗独立盲测对照，共 100 颗。科学目标满足 `Tmag < 11.5` 且至少两个扇区具备 SPOC 120 秒光变。用 TIC 连接恒星属性和实际可下载产品，按去趋势后的小时尺度散度与覆盖率排序，优先 G/K/M 矮星。盲测对照与黄金样本分离，搜索和评分过程只读取统一输入清单，冻结并记录排序和文件哈希后才读取 `blind_key.json`。
- `search_transits.py` 的周期通道搜索 0.8–30 天、1–8 小时，记录周期、历元、持续时间、深度、各次事件覆盖、SNR 和 SDE。固定并报告 SNR、SDE 定义与计算参数。阈值初始设为 SNR ≥7.5、SDE ≥6.5。至少两次独立且覆盖充分的事件才能进入周期候选，同时保留主要谐波供诊断。
- 单次通道在连续数据段用 2、4、8 小时箱型模板搜索，使用局部基线和局部噪声估计深度显著性。报告相邻缺口、窗口覆盖、耀斑和质量标记，跨宽度合并重叠触发。用实际时间窗检查同类事件的可观测性。单次事件仅报告时刻和覆盖约束，周期留空。
- `vet_candidates.py` 各项输出 `PASS`、`FLAG`、`INCONCLUSIVE` 或 `NOT_APPLICABLE`，附观测覆盖和统计量。周期事件测奇偶深度差与相位 0.5 次食，数据覆盖不足置为 `INCONCLUSIVE`。单次事件的两项相位检查置为 `NOT_APPLICABLE`。U/V 谷形作为证据，低信噪比无法区分时置为 `INCONCLUSIVE`。
- 使用 SPOC TPF 多孔径深度、凌星内外差分图与质心偏移评估污染。SAP 与 PDCSAP 深度差可作为通量处理敏感性指标。TPF 缺失或信号太弱时，孔径/质心项置为 `INCONCLUSIVE`。
- 星表先按 TIC 检索，再对信号匹配周期及观测历元，并检查主要谐波及 TOI disposition。单次事件用事件时刻与已知星历匹配，星历不确定度太大时标为待核实。仅在两表查询完整且无相容事件时标为 `UNCATALOGED`。另存宿主已知状态，查询失败标为 `CATALOG_UNAVAILABLE`。

## Stage 4 · 交付与复核

- 输出 `/home/data/astro_tess/out/batch_001/` 并同步到 `D:\SPARK\astro_out\batch_001\`。交付 `candidates_ranked.csv`、`candidates_ranked.json`、`batch_summary.md`、输入清单和前 15 条候选的四联图。盲测解盲后附对照排名、恢复周期及误差，未触发的对照单列并给出诊断图。
- 周期图包含原始与去趋势序列、折叠图、奇偶图、代表性单次事件局部图。单次图包含原始与去趋势序列、事件及局部基线、箱型或梯形模型残差、像素诊断或缺失说明。
- 摘要报告处理数、耗时、磁盘占用、各通道的筛查数量、对照恢复情况和缺证项。两颗对照仅用于首批回归，回收率和误报率应基于有覆盖率分层的注入恢复与负对照。允许零个新候选。`UNCATALOGED` 信号均称为待复核凌星候选信号，在 [ExoFOP TESS](https://exofop.ipac.caltech.edu/tess/) 核对 TOI、社区候选和邻近污染记录。模型服务仅在用户另行授权后才用于离线复核。

## 本次预检记录

- NASA TAP `ps` 返回 7 条已确认系统行星记录，`toi` 用 `tid` 返回 5 条 TOI 记录。四颗周期验收行星的结果已冻结在 [golden-ephemerides.json](../astro_tess/golden-ephemerides.json)。
- Windows 直连 MAST 主站间歇出现 TLS 握手失败和超时。`Mast.Caom.Filtered` 实查返回 SPOC 的 20 秒及 120 秒观测，验证了按 `t_exptime` 筛选的必要性。经 Jetson 查询 `Mast.Catalogs.Filtered.Tic` 返回两颗目标的星等、温度、表面重力和矮星分类。
- MAST 的 AWS 公开镜像通过 S3 目录列举及 FITS 下载验证。Windows 下载部分光变，其余经 Jetson 的已验证 SSH 链路转回 Windows。共核验 π Men S1 和 TOI-700 11 个扇区的 120 秒光变，以及 π Men S1 的 93,352,320 字节像素文件。见 [光变输入清单](../astro_tess/tess-golden-inputs.json)和 [像素下载抽查](../downloads/tess-probe.json)。
- Jetson USB 只读检查确认 aarch64，根分区约剩 32 GiB，独立可写 NVMe 约剩 803 GiB。8080 无监听，两个指定服务均为 `inactive/disabled`。

## 本次 Stage 0 结果

两颗目标的真实周期均为 BLS 第一名并达到原提示词 0.5% 周期误差指标。本次审核版让黄金样本复用批处理的 SNR ≥7.5 和 SDE ≥6.5 门槛。按此门槛，π Men c 未通过，流程停在 Stage 0。

| 目标 | 检出周期（天） | 周期误差 | 有效事件 | SNR | SDE |
| --- | ---: | ---: | ---: | ---: | ---: |
| π Men c | 6.26798042 | 0.00251% | 4 | 51.01 | 4.72 |
| TOI-700 c | 16.05097366 | 0.00081% | 12 | 26.74 | 24.58 |

π Men 只用了 S1。SDE 由全搜索区间的 BLS 深度信噪比谱均值和标准差归一化，12.54 天、3.13 天等谐波也给出强峰。优先追加 π Men 的 120 秒观测扇区，保持门槛和处理参数再测，检验观测基线对 SDE 的影响。单次通道复现了注入的 4 小时事件，未触发缺口边缘注入。窗口显著性用侧窗噪声估计的深度标准误作分母，阈值为 5.5。一次强注入只验证检测和缺口处理路径。

Jetson 回归报告、两张折叠图及环境锁定清单已同步到 [astro_out/golden](../astro_out/golden/)。运行后复查根分区约剩 32 GiB，NVMe 约剩 801 GiB，两个指定服务仍为 `inactive/disabled`，8080 无监听。
