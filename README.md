# TESS Exoplanet Hunter (v2.2)

> **[English](./README.md)** | **[简体中文](./README_ZH.md)**  
> An autonomous, end-to-end **TESS 2-minute SPOC exoplanet transit mining & deep-vetting pipeline** running on **Jetson AGX Orin** + a zero-dependency **Windows local Observatory Dashboard**.

---

## 1. Overview & Scientific Highlights

Finding exoplanet transits in NASA TESS data is less about "finding dips" and more about **rejecting 99% of astrophysical and instrumental false positives** (stellar spot rotation, eclipsing binaries, background blends, momentum dumps, and bright-star saturation).

**v2.2 Hardened Pipeline** combines four proven open-source / literature methodologies into a fully automated, unattended loop:

1. **SHERLOCK-Inspired Rotational Pre-Whitening (`scripts/tess_v21.py`)**  
   Fits and removes $f + 2f$ Fourier rotational modes on raw normalized sector light curves *before* detrending, cleanly suppressing active M/K dwarf spot modulations. Deep vetting additionally checks multi-detrending consistency (`med_24h`, `savgol_24h`, `med_12h`).
2. **NASA ExoMiner++ 2.0 Physical & Statistical Gates (`scripts/tess_v21.py`, `scripts/tess_vetting.py`)**  
   - Circular Keplerian equatorial transit duration ratio guard: $D / T_{\text{circ}} \le 1.8$ (`keplerian_duration_ratio`).
   - Strict BLS detection threshold: `SNR >= 7.5` and `SDE >= 6.5` (no low-SDE bright-star bypass).
   - Smooth-profile vs. U-shape $\Delta\text{BIC}$ test (`_smooth_delta_bic`) and **full-phase secondary scan** ($\phi \in [0.1, 0.9]$) to intercept eccentric eclipsing binaries and $3P/4P/5P$ rotational harmonic aliases.
3. **Planet Hunters TESS (PHT) / NotPlaNET Single-Transit Support (`scripts/tess_v21.py`, `scripts/rolling_scanner.py`)**  
   Dedicated isolated single-transit search with shoulder-step and same-sector isolation guards (`< 3` deep dips per 27-day sector), plus up to 3 reserved Top-15 TPF pixel-vetting slots for long-period single transits.
4. **Hidden Gems 2026 Target Selection (`--target-pool hybrid`, `scripts/rolling_sources.py`)**  
   Each 100-target science batch pairs **50 known-planet M/K dwarf hosts** (from a 9,092-star NASA `PS+TOI` pool, searching for smaller missed companions) with **50 latest-sector cross-year standard targets**, plus **2 hidden blind-control planets** (`WASP-18 b`, `HIP 65 A b`) injected into every batch to continuously verify recall.

---

## 2. Repository Structure

```text
tess-exoplanet-hunter/
├── README.md                    # English documentation (default)
├── README_ZH.md                 # Chinese documentation (简体中文)
├── LICENSE                      # MIT License
├── .env.example                 # SSH & Jetson environment variable template
├── scripts/                     # Core search, vetting, deep-vet & Jetson sync scripts
│   ├── tess_v21.py              # BLS iterative search, SHERLOCK pre-whitening, Kepler ratio, single-transit search
│   ├── tess_vetting.py          # Odd/even, full-phase secondary, U/V & smooth ΔBIC, catalog & TPF pixel vetting
│   ├── rolling_sources.py       # MAST / NASA TAP target selection (hybrid / hidden_gems / standard)
│   ├── rolling_scanner.py       # 100-star rolling batch supervisor, blind controls, Top-15 TPF & high-priority ranking
│   ├── deep_vet.py              # Deep-vetting orchestrator (+ _photometry / _catalog / _pixels)
│   ├── jetson_job.py            # Windows → Jetson SSH/SFTP deployment & control CLI
│   ├── v21_sync.py              # Incremental SSH report streamer
│   └── ...
├── web/                         # Zero-dependency local Observatory Dashboard (127.0.0.1:18805)
│   ├── server.py                # Python stdlib read-only HTTP server + path-traversal guard + local triage notes
│   └── index.html / style.css / app.js
├── outputs/v21/                 # Showcase verification artifacts
│   ├── v21_golden/              # 4/4 golden planet regression report & folded plots
│   ├── deep_vet_top10/          # B2–B6 42-signal deep-vetting summary (0 KEEP_TOP)
│   └── deep_dive_381949122/     # TIC 381949122 (ζ Dor A, 17.0511 d) Phase-1 falsification report & 4-panel plot
├── start-dashboard.ps1          # Launch local dashboard & open browser
├── start-scan.ps1               # Standalone scan controller (-Batches / -StopOnHit / -Stop / -Status)
└── *.bat                        # Windows double-click shortcuts (run 5 batches / stop-on-hit / pause / dashboard)
```

---

## 3. Verification & Benchmark Results (v2.2)

### Four-Gate Regression Suite (All PASS)
| Gate | Scope | Result |
| :--- | :--- | :--- |
| **Gate 1 (Golden Controls)** | `v21-golden` ([golden_report.json](./outputs/v21/v21_golden/golden_report.json)) | **PASS**: 4/4 golden planets recovered (`TOI-700 c/b/d`, `pi Men c`), `single_injection = True`, 0 false flags under full-phase secondary scan (`max_off_phase_sig = 0.61–2.72`) |
| **Gate 2 (Offline Replay)** | B2–B6 0-byte replay (blind controls + 33 FP TICs / 42 v2.1 alerts) | **PASS**: Blind controls **2/2** (`WASP-18 b` SDE `17.64`, `HIP 65 A b` SDE `21.09`); **periodic FPs reduced from 42 → 0 (100% periodic FP elimination)** |
| **Gate 3 (Hidden Gems)** | NASA `PS+TOI` host pool & latest MAST sector dry-run | **PASS**: Cached 9,092 hosts; verified latest sector S108 and cross-year M/K dwarf host selection |
| **Gate 4 (Operational Safety)** | Disk, flock, sentinel, ports & services | **PASS**: Verified root/NVMe free space thresholds, lock exclusivity, and clean resource isolation |

### Live Batch 7 Trial (`hybrid` mode: 50 Hidden Gems + 50 Standard + 2 Blind Controls)
- **Blind Controls**: **`2/2 PASS`** (`WASP-18 b` period error `0.00008%`, `HIP 65 A b` `0.00013%`).
- **Small-Planet Sensitivity**: Independently recovered **21 confirmed planets / TOIs** across the 50 Hidden Gems hosts, down to sub-Earth radii (**`0.69 R⊕`** on `TIC 369327947`, **`0.90 R⊕`** on `TIC 410153553`).
- **False-Positive Suppression**: **`0` false high-priority alerts** across the entire 100-target batch; `903` unneeded raw FITS files auto-pruned (`0.648 GiB` total transfer).

### Deep-Vetting Case Study Showcase
- **`TIC 381949122` ($\zeta$ Dor A, `17.0511 d`)**: Falsified at **0 bytes downloaded** in Phase 1 as a **5× differential rotation harmonic alias** ($17.0511 / 5 = 3.4102\text{ d}$, `42.5σ` off-phase troughs every $\Delta\phi = 0.20$, `savgol_24h` collapse to `43 ppm / 1.4–1.7σ`, BLS `SDE = 2.83`). Full report: [REPORT_TIC381949122.md](./outputs/v21/deep_dive_381949122/REPORT_TIC381949122.md).

---

## 4. Quick Start

### 4.1 Configure SSH Credentials (Never Commit Real Credentials)
Copy `.env.example` or set system environment variables:
```powershell
$env:TESS_JETSON_HOST = "192.168.55.1"
$env:TESS_JETSON_USER = "your_username"
$env:TESS_JETSON_PASS = "your_password"
```

### 4.2 Run the Local Observatory Dashboard (Read-Only, `127.0.0.1:18805`)
```powershell
py -3.11 web/server.py
# Or on Windows:
powershell -ExecutionPolicy Bypass -File start-dashboard.ps1
```

### 4.3 Run Standalone Scans (No AI Required)
```powershell
# Check Jetson status, disk & lock
powershell -ExecutionPolicy Bypass -File start-scan.ps1 -Status

# Run next 5 batches (500 science targets) in hybrid mode, then auto-pause
powershell -ExecutionPolicy Bypass -File start-scan.ps1 -Batches 5 -Pool hybrid

# Stop immediately when a high-priority candidate is discovered
powershell -ExecutionPolicy Bypass -File start-scan.ps1 -Batches 10 -Pool hybrid -StopOnHit

# Gracefully pause after the current batch
powershell -ExecutionPolicy Bypass -File start-scan.ps1 -Stop
```

---

## 5. Reporting Verified Candidates to the Astronomy Community

Only submit candidates that are **`UNCATALOGED`** *and* pass full deep vetting (**`KEEP_TOP`**, ruling out stellar rotation, eclipsing binaries, background contamination, and momentum dumps):

1. **ExoFOP-TESS (NASA IPAC)** — https://exofop.ipac.caltech.edu/tess/  
   Submit a **CTOI (Community TESS Object of Interest)** with period $P$, epoch $T_0$ (BTJD), duration, depth (ppm), and diagnostic plots for official TFOP follow-up and potential promotion to **TOI**.
2. **Planet Hunters TESS (Zooniverse)** — https://www.zooniverse.org/projects/nora-dot-eisner/planet-hunters-tess/talk  
   Share the `TIC ID`, folded transit plot, and vetting evidence on Talk for peer review by experienced citizen scientists and professional astronomers.
3. **RNAAS / Collaborative Follow-Up**  
   High-value discoveries (e.g., habitable-zone or bright-star multi-planet companions) can be published as a Research Note of the AAS (RNAAS) or followed up collaboratively with teams on ExoFOP.

---

## License

MIT License — see [LICENSE](./LICENSE).
