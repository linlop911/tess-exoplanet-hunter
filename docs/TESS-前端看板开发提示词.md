# TESS 系外行星搜星看板（前端网页）开发提示词

> **适用项目**：`d:\SPARK\tess_exoplanet\`  
> **建议位置**：`d:\SPARK\tess_exoplanet\web\`（独立子目录，不侵入 `scripts\` 与 `outputs\`）  
> **用法**：将下方「完整执行提示词」直接发给 AI 编码代理（Antigravity / Codex / Claude Code 等），审核方案后即可一键落地。

---

## 一、先说结论：要不要做前端网页？

**建议做，但务必做「轻量本地只读看板」，不要做重型前后端框架。**

- **为什么值得做**：
  1. 随着 Batch 7+ 推进，每个批次会产生 `batch_status.json`、`candidates_ranked.json`、`high_priority.json` 和最多 15 张四联诊断图（`candidate_01..15.png`），外加深度证伪结果（`deep_vet_top10`、`deep_dive_*`）。靠翻文件夹看 CSV/PNG 效率很低。
  2. 轻量看板能让你 **5 秒看清**：Jetson 当前在跑哪一批、盲控是否 2/2 正常、Hidden Gems vs 标准池各出了多少高优候选、直接筛选/排序并放大审阅四联图，还能一键标记“值得深挖”的 TIC 记入本地审阅清单。
- **为什么不要做重型架构**：
  - 不需要 React/Vite/Node 构建链、不需要数据库、不需要公网暴露。
  - 直接用 **Windows `py -3.11` 标准库 `http.server` + 单页原生 HTML/CSS/JS**（参考 `d:\SPARK\tools\jetson_twin_web\` 的零依赖风格），只读挂载 `d:\SPARK\tess_exoplanet\outputs\v21\`，绑定 `127.0.0.1:18805`，开箱即用、零维护成本。

---

## 二、完整执行提示词（可直接复制发给 AI）

```markdown
请在 `d:\SPARK\tess_exoplanet\web\` 下为 TESS 系外行星自动挖掘流水线（v2.2）开发一个**轻量级、零前端构建依赖的本地天文观测看板（TESS Observatory Dashboard）**。

### 1. 硬性约束与安全边界
1. **只读隔离**：后端仅读取 `d:\SPARK\tess_exoplanet\outputs\v21\` 与 `d:\SPARK\tess_exoplanet\logs\` 下的现有 JSON/CSV/MD/PNG 文件；**严禁**修改、删除任何扫描产出文件，**严禁**触碰 Jetson SSH 凭据或执行任何远端控制命令（除非用户显式要求，本版本仅做本地审阅，唯一允许写入的是 `d:\SPARK\tess_exoplanet\web\triage_notes.json` 用于保存用户的本地人工复核标记）。
2. **仅本机监听**：HTTP 服务严格绑定 `127.0.0.1:18805`（禁止 `0.0.0.0`），并提供安全路径校验（防 `..` 目录穿越，只允许访问 `outputs/v21/` 内的白名单扩展名 `.png/.json/.csv/.md`）。
3. **零重型依赖**：后端仅使用 Windows `py -3.11` Python 标准库（`http.server` / `json` / `csv` / `pathlib` / `urllib`）；前端使用原生 HTML5 + CSS3 + ES6 JS（单页应用，无 npm/webpack/CDN 外链依赖，离线可用）。
4. **不破坏现役任务**：不得修改 `d:\SPARK\tess_exoplanet\scripts\` 下任何扫描或同步脚本，不得影响计划任务 `\SPARK-TESS-V21-ReportSync`。

### 2. 数据源映射（全部来自 `d:\SPARK\tess_exoplanet\outputs\v21\`）
后端提供轻量 JSON API（如 `/api/overview`、`/api/batches`、`/api/batch/<id>`、`/api/candidates`、`/api/deep-vet`、`/api/triage`）及静态图接口 `/files/<relpath>`：
- **全局与同步健康**：`sync_status.json`、`v21_monitor.json`、`scanner_state.json`、`STOP_REASON.txt`、`v21_golden/golden_report.json`
- **批次列表与详情**：自动扫描 `v21_batch_00001` .. `v21_batch_NNNNN` 下的 `batch_status.json`、`candidates_ranked.json`、`high_priority.json`、`retention.json`、`candidate_*.png`
- **汇总候选池**：`MASTER_HITS.json` / `MASTER_HITS.csv`
- **深度证伪归档**：`deep_vet_top10/MASTER_HITS_deep_vetted.csv`、`deep_vet_top10/DEEP_VET_REPORT.md`，以及自动发现 `deep_dive_*` 目录下的 `REPORT_*.md` 与 `DIAGNOSTIC_*.png`

### 3. 核心页面模块与交互设计（深空天文暗色主题）
1. **顶部实时状态栏（每 15 秒自动刷新，可手动点击刷新）**：
   - 流水线状态徽章（运行中 / 已暂停 `STOP` / 同步异常）、当前批次号、已扫星总数、最近同步时间、黄金回归门禁状态（4/4 PASS + SHA 前 8 位）。
2. **总览 KPI 卡片 + 批次时间线**：
   - 总扫描目标数、盲控累计召回率（如 `10/10`）、高优候选总数（拆分为 `periodic` 周期信号 vs `single` 单凌星）、Hidden Gems vs Standard 目标统计。
   - 批次列表卡片：每批展示批次号、完成时间、盲控结果（绿色 PASS / 红色 FAIL）、高优数量、Top-15 数量，点击可下钻到该批详情。
3. **候选体筛选与审阅台（核心工作区）**：
   - 支持按 **批次**、**类型（periodic / single）**、**是否高优**、**目录状态（UNCATALOGED / KNOWN_TOI / CONFIRMED / KNOWN_EB）**、**最小 SNR / SDE**、**关键词（TIC ID）** 实时筛选与排序。
   - 表格清晰展示：`TIC`、`批次`、`类型`、`周期 P (d)`、`深度 (ppm)`、`SNR`、`SDE`、`估算半径 Rp (R⊕)`、`Gaia RUWE`、`关键核验徽章（奇偶/全相位次级/形状ΔBIC/Kepler时长比/像素质心）`、`人工复核状态`。
   - **侧边/弹窗四联图检视器（Lightbox）**：点击任意候选行，右侧/弹窗展示对应的 `candidate_XX.png` 四联诊断图（支持滚轮/点击放大原图、键盘 `↑/↓` 快速切上一颗/下一颗星）、完整 JSON 核验明细，以及**本地人工复核标记按钮**（`待深挖` / `疑似真行星` / `已排除` + 简短备注，持久化保存到 `web/triage_notes.json`）。
4. **深度证伪档案页（Deep Vet & Deep Dive）**：
   - 展示 `deep_vet_top10` 的分级统计（`KEEP_TOP` / `BORDERLINE` / `REJECT`）与归因分布，并直接预览 `deep_dive_*`（如 `TIC 381949122`）的诊断图与 Markdown 结论摘要。

### 4. 启动脚本与交付验收
1. 交付文件：
   - `d:\SPARK\tess_exoplanet\web\server.py`（本地只读服务 + 安全静态文件代理 + `triage_notes.json` 读写）
   - `d:\SPARK\tess_exoplanet\web\index.html`、`style.css`、`app.js`
   - `d:\SPARK\tess_exoplanet\start-dashboard.ps1`（一键用 `py -3.11` 启动服务并自动打开浏览器 `http://127.0.0.1:18805/`）
2. 验收标准：
   - `py -3.11 d:\SPARK\tess_exoplanet\web\server.py` 启动无报错，仅监听 `127.0.0.1:18805`；
   - 实测 `/api/overview`、`/api/batches`、`/api/candidates`、`/api/deep-vet` 返回 HTTP 200 且数据与磁盘真实文件一致；
   - 路径穿越测试（如 `/files/../../Jetson-SSH登录信息.txt`）严格返回 `403/404`；
   - 浏览器中能流畅浏览 B1–B7 批次数据、放大查看四联图、切换深验档案并保存人工复核备注。
```
