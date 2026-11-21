# TESS 系外行星自动挖掘与深度验证项目（v2.2 加固版）

> **[English](./README.md)** | **[简体中文](./README_ZH.md)**  
> **项目目录**：`d:\SPARK\tess_exoplanet\`（Windows 控制与归档端） ↔ Jetson AGX Orin `/home/data/astro_tess/`（无人值守计算端）  
> **最后更新**：2026-11-21  
> **当前状态**：**v2.2 加固版已完成 Batch 7（`hybrid` 模式试水）并自动停机（累计已扫 700 颗科学目标星），本地看板运行于 `http://127.0.0.1:18805/`，随时可启动 Batch 8+。**

---

## 1. 项目目标与差异化路线

本项目构建了一条 **Windows AI Agent 调度 + Jetson AGX Orin 无人值守批量搜星与假阳性排除 + 自动报告同步与深度证伪** 的端到端 TESS（2 分钟短曝光 SPOC 光变曲线）系外行星候选体挖掘流水线，重点吸收并工程化落地了三大开源/前沿方法：

1. **SHERLOCK 思路**：去趋势前在原始归一化光变曲线上做 $f + 2f$ 傅里叶消旋（`prewhiten_curve`），消除活跃红/橙矮星与快自转恒星的周期性黑子调制；深验阶段做多去趋势方法（`med_24h` / `savgol_24h` / `med_12h`）一致性检验。
2. **NASA ExoMiner++ 2.0 思路**：引入圆轨道开普勒赤道凌星持续时间上限约束 $D / T_{\text{circ}} \le 1.8$（`keplerian_duration_ratio`）、平滑轮廓 $\Delta\text{BIC}$ 检定（`_smooth_delta_bic`）与 $\phi \in [0.1, 0.9]$ 全相位次级扫描，拦截食双星与 $3P/4P/5P$ 自转谐波。
3. **Planet Hunters TESS (PHT) / NotPlaNET 思路**：修复单凌星排序并为其保留 Top-15 TPF 核验席位，增加肩部阶跃检查（`shoulder_step_sigma`）与同扇区孤立检查（27 天扇区内深谷 $< 3$ 个），同时恢复卫星动量卸载/窗口边缘的真实凌星事件。
4. **Hidden Gems 2026 选星策略（`--target-pool hybrid`）**：每批 100 颗科学目标默认采用 **50 颗已知行星宿主红/橙矮星（NASA `PS+TOI` 9,092 颗池，寻找更微弱的伴星）+ 50 颗最新扇区标准目标**，每批自动混入 2 颗盲控已知行星以持续校验流水线召回率。

---

## 2. 目录归类结构

```text
d:\SPARK\tess_exoplanet\
├── README.md                    # 本文件：项目总览、算法说明、状态与运行手册
├── scripts\                     # 全部扫描、深验、同步与 Jetson 调度代码
│   ├── tess_v21.py              # [核心] BLS 搜索、SHERLOCK 去旋、Kepler 持续时间比、单凌星搜索、黄金回归
│   ├── tess_vetting.py          # [核心] 奇偶检验、全相位次级扫描、U/V 与平滑 ΔBIC、目录与 TPF 像素核验
│   ├── rolling_sources.py       # [核心] MAST / NASA TAP 选星（hybrid / hidden_gems / standard）与断点下载
│   ├── rolling_scanner.py       # [核心] 100 星滚动批次扫描、盲控校验、Top-15 TPF、高优判定与归档
│   ├── deep_vet.py              # [深验] 深度证伪主控（+ deep_vet_photometry.py / _catalog.py / _pixels.py）
│   ├── jetson_job.py            # [控制] Windows → Jetson SSH/SFTP 部署、状态查询、日志与导出总入口
│   ├── v21_sync.py              # [同步] SSH 增量流式同步（由计划任务 \SPARK-TESS-V21-ReportSync 守护）
│   ├── v21_monitor.py           # [监控] Jetson 侧健康监控与告警生成
│   ├── v21_increment.py         # [增量] 批次增量推进脚本
│   ├── v21_notification.ps1     # [通知] Windows Toast 桌面通知脚本
│   ├── golden_calibration.py    # 黄金标定底层工具（detrend / phase / single_search / read_curve）
│   ├── tess_sources.py          # MAST/AWS 公共归档与数据校验工具
│   ├── tess_campaign.py         # 早期 1000 星单次战役脚本（保留参考）
│   ├── rolling_smoke.py         # 滚动扫描冒烟测试脚本
│   ├── diagnose_sectors.py      # 扇区诊断辅助脚本
│   ├── bootstrap_jetson.sh      # Jetson 环境初始化脚本
│   ├── golden-ephemerides.json  # 黄金行星星历基准
│   ├── tess-golden-inputs.json  # 黄金测试集输入清单
│   └── v21_backup\              # v2.2 升级前的 v2.1 代码完整备份
├── outputs\                     # 本地同步的扫描与深验产出
│   ├── golden\ / golden_expanded\ # 早期黄金标定产出
│   └── v21\
│       ├── v21_golden\          # 最新 v2.2 黄金回归报告（golden_report.json + 折叠图）
│       ├── v21_batch_00001..6\  # Batch 1–6 扫描报告、候选表与四联图
│       ├── deep_vet_top10\      # B2–B6 42 条高优信号的 v2+A2 深度证伪终版（0 KEEP_TOP）
│       ├── deep_vet_top10_v1\   # 深验 v1 历史备份
│       ├── deep_vet_qa_preliminary\ # 初筛归档图
│       ├── deep_dive_381949122\ # TIC 381949122 (17.0511d) 第一阶段 0 字节证伪完整报告与诊断图
│       └── MASTER_HITS.* / HIT_ALERT_*.md / sync_status.json / v21_monitor.json
├── web\                         # [看板] 本地只读天文观测看板（127.0.0.1:18805，零构建依赖）
│   ├── server.py                # Python 标准库 HTTP 服务 + 路径防穿越 + 本地人工复核标记
│   └── index.html / style.css / app.js
├── start-dashboard.ps1          # 一键启动看板并打开浏览器 http://127.0.0.1:18805/
├── start-scan.ps1               # 独立控制台脚本（支持 -Batches / -StopOnHit / -Stop / -Status，含安全预检）
├── 启动扫描-跑5批.bat            # [双击即用] 脱离 AI 自动接着跑下 5 批（500 星，hybrid 模式）并打开看板
├── 启动扫描-挖到高优即停.bat      # [双击即用] 脱离 AI 自动跑最多 10 批，挖到高优候选立刻停机提醒
├── 暂停扫描.bat                 # [双击即用] 写入 Jetson STOP 哨兵优雅停机
├── 打开看板.bat                 # [双击即用] 启动本地看板并打开浏览器
├── docs\                        # 本项目专属说明与历史审核文档
│   ├── TESS-前端看板开发提示词.md
│   ├── TESS-V2.1-Jetson-运行与验收.md
│   ├── TESS-Jetson-1000星运行说明.md
│   └── TESS-候选信号挖掘-审核版提示词.md
└── logs\                        # 本地同步日志（tess-v21-sync.log）
```

> **凭据位置**：`jetson_job.py` 自动回退读取上级工作区的 `d:\SPARK\Jetson-SSH登录信息.txt`，本项目目录内**不复制**明文凭据。

---

## 3. v2.2 加固版验证基线（2026-11-21）

### 3.1 代码部署一致性（本地与 Jetson SHA-256 完全一致）
- `tess_v21.py`：`835c8400e1aa...`（严格 `snr >= 7.5 and sde >= 6.5`、去趋势前 $f+2f$ 消旋、$D/T_{\text{circ}} \le 1.8$、单凌星肩部与孤立检查）
- `tess_vetting.py`：`54fba172a13d...`（平滑轮廓 $\Delta\text{BIC}$、$\phi \in [0.1, 0.9]$ 全相位次级扫描、Hidden Gems 目录兼容）
- `rolling_sources.py`：`2cf9e83f306c...`（9,092 颗已知行星宿主池、`hybrid` 默认选星）
- `rolling_scanner.py`：`3a28b443ecf0...`（单凌星 Top-15 TPF 席位、`kepler_duration` 门禁）

### 3.2 四项门禁回归结果（全部 PASS）
| 门禁 | 验证内容 | 结果 |
| :--- | :--- | :--- |
| **门禁 1（黄金回归）** | `v21-golden`（[golden_report.json](./outputs/v21/v21_golden/golden_report.json)） | **PASS**：4/4 黄金行星召回（`TOI-700 c/b/d`、`pi Men c`），`single_injection = True`，全相位次级扫描 0 误杀（`max_off_phase_sig = 0.61–2.72`） |
| **门禁 2（离线回放）** | B2–B6 0 字节回放（盲控 + 33 颗 FP 目标） | **PASS**：盲控 **2/2**（`WASP-18 b` SDE 17.64、`HIP 65 A b` SDE 21.09）；42 条 v2.1 高优信号降为 3 条单凌星，**周期性假阳性清零（`periodic = 0`）** |
| **门禁 3（Hidden Gems）** | NASA `PS+TOI` 宿主池与最新扇区干跑 | **PASS**：成功缓存 9,092 颗宿主星，识别最新扇区 S108 与跨年红/橙矮星候选 |
| **门禁 4（Jetson 边界）** | 磁盘、锁、哨兵、端口、服务 | **PASS**：`STOP` 存在、`rolling_v21.lock` 空闲、`/` 剩余 `32 GiB`、NVMe 剩余 `796 GiB`、`:8080` 未监听、两项无关服务均 `inactive/disabled` |

### 3.3 已完成的实战与深度证伪归档
- **Batch 7（v2.2 加固版 + `hybrid` 混合池首战）**：[outputs/v21/v21_batch_00007/](./outputs/v21/v21_batch_00007/) — 100 颗科学目标（50 Hidden Gems + 50 标准）+ 2 颗盲控（**`2/2 PASS`**），独立召回 21 条已知行星/TOI（含 `0.69 R⊕`、`0.90 R⊕` 亚地球级小行星），虚假高优压至 `0` 条，自动清理 `903` 个无用原始文件。
- **B2–B6 42 条高优信号（v2+A2 深度证伪）**：[outputs/v21/deep_vet_top10/](./outputs/v21/deep_vet_top10/) — `0 KEEP_TOP` / `4 BORDERLINE` / `38 REJECT`（黄金对照 `pi Men c` 干净 SNR `32.47`、`TOI-700 c` `14.53`）。
- **`TIC 381949122`（$\zeta$ Dor A，`17.0511 d`）**：[REPORT_TIC381949122.md](./outputs/v21/deep_dive_381949122/REPORT_TIC381949122.md) — 第一阶段 0 字节即证伪为 **5 倍自转谐波混叠**（`17.0511 / 5 = 3.4102 d`，`savgol_24h` 坍塌至 `43 ppm / 1.4–1.7σ`，BLS `SDE = 2.83`）。

---

## 4. 常用操作命令（Windows 侧一律用 `py -3.11`）

```powershell
# 1. 启动本地只读看板（http://127.0.0.1:18805/）
powershell -ExecutionPolicy Bypass -File d:\SPARK\tess_exoplanet\start-dashboard.ps1

# 2. 查看 Jetson 侧状态
py -3.11 d:\SPARK\tess_exoplanet\scripts\jetson_job.py v21-status

# 3. 在 Jetson 上执行只读检查
py -3.11 d:\SPARK\tess_exoplanet\scripts\jetson_job.py command "ls -l /home/data/astro_tess/STOP; df -BG / /home/data"

# 4. 同步上传 4 个核心脚本并重跑黄金回归（约 90 秒）
py -3.11 d:\SPARK\tess_exoplanet\scripts\jetson_job.py v21-golden

# 5. 核验 Windows 自动同步计划任务
schtasks /query /tn "\SPARK-TESS-V21-ReportSync" /v /fo LIST | Select-String "TaskName|Status|Task To Run"
```

### 安全与运行红线
1. **未经用户明确指令，禁止删除 `/home/data/astro_tess/STOP` 或擅自启动新批次**。
2. **禁止并发运行多个 `jetson_job.py` / SFTP 会话**（USB-RNDIS 并发易断开 10054）。
3. **任何核心算法改动后**，必须同步上传 Jetson、核验本地与远端 SHA-256 一致、并跑通 `v21-golden`。
4. 启动 **Batch 8+**（需用户授权）时，以 `--target-pool hybrid --last-batch <N>` 启动 `rolling_scanner.py`。

---

## 5. 数据更新节奏与发现成果提交渠道

### 5.1 无需每天拉数据
- TESS 卫星约每 **27.4 天（~1 个月）** 放出 1 个新扇区，不需要每天拉取。
- MAST 历史存量极大（100+ 扇区、数十万颗星、9,092 颗已知行星宿主池）；`scanner_state.json` 会自动记录已扫 `TIC ID` 避免重复，且每批跑完自动清理无用原始大文件。
- **推荐节奏**：平时按需批量跑存量（例如每次跑 5 批 = 500 颗星，跑完自动停）；每月等 TESS 放出新扇区后再专门追跑新扇区。

### 5.2 挖到真正候选行星后提交给谁？
> **提交前提**：常规批次产出**不要**直接上报；**仅当候选体同时满足 `UNCATALOGED`（未收录）+ 通过深度证伪（`KEEP_TOP`、排除恒星自转/食双星/背景污染/动量卸载伪影）** 时，才对外提交，维护个人发现者信誉。

| 优先级 | 渠道 | 网址与方式 | 作用与署名 |
| :--- | :--- | :--- | :--- |
| **① 最核心（拿正式编号）** | **ExoFOP-TESS**（NASA IPAC 官方平台） | https://exofop.ipac.caltech.edu/tess/ <br>注册免费账号，在对应 `TIC` 页面提交 **CTOI**（填 $P$、$T_0$、时长、深度 ppm 并附诊断图） | 经 NASA TFOP 复核通过后升级为正式 **TOI**，全球地面望远镜接力确认，保留个人上报/发现署名 |
| **② 先让同行把关** | **Planet Hunters TESS**（Zooniverse 社区） | https://www.zooniverse.org/projects/nora-dot-eisner/planet-hunters-tess/talk <br>在 Talk 发帖贴出 `TIC ID`、参数、四联图与排假证据 | 资深猎手与专业天文学家交叉复核，确认过硬后可协助推送至 ExoFOP 升级 CTOI/TOI |
| **③ 高价值目标发论文** | **RNAAS / 专业团队合作** | 向美国天文学会研究简报（RNAAS）投稿，或在 ExoFOP 联系跟拍该星的课题组 | 针对宜居带/亮星/多行星伴星等罕见发现，以独立研究者/共同作者身份发表论文 |
