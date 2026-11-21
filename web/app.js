const state = {
  overview: null,
  candidates: [],
  sortKey: 'snr',
  sortDir: -1,
  activeIndex: -1,
  triageStatus: 'unreviewed',
};

const $ = (id) => document.getElementById(id);

function fmt(n, digits = 2) {
  return n === null || n === undefined || Number.isNaN(Number(n)) ? '—' : Number(n).toFixed(digits);
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 2400);
}

function triageLabel(s) {
  return { deep_dive: '★ 待深挖', promising: '✓ 疑似真行星', rejected: '✕ 已排除', unreviewed: '未复核' }[s] || s;
}

function triageClass(s) {
  return { deep_dive: 'b-violet', promising: 'b-pass', rejected: 'b-flag', unreviewed: 'b-na' }[s] || 'b-na';
}

function switchTab(tabName) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tabName));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `tab-${tabName}`));
  if (tabName === 'candidates') loadCandidates();
  if (tabName === 'deepvet') loadDeepVet();
}

async function fetchJson(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function loadOverview() {
  const data = await fetchJson('/api/overview');
  state.overview = data;
  const sync = data.sync || {};
  const mon = data.monitor || {};
  const k = data.kpis || {};
  const g = data.golden || {};

  const running = Boolean(sync.rolling_active);
  const dot = $('liveDot');
  dot.className = 'dot ' + (running ? 'running' : 'paused');
  $('statusSub').textContent = running
    ? `Jetson 运行中 · 正在处理 Batch ${sync.next_batch ?? '--'} · 已提交 ${sync.science_targets_committed ?? k.science_targets} 颗目标星`
    : `已暂停 · 下一批 Batch ${sync.next_batch ?? '--'} · ${data.stop_reason || 'STOP 哨兵生效'}`;

  const gp = $('goldenPill');
  gp.textContent = `黄金门禁: ${g.passed ? 'PASS' : 'FAIL'} (${g.matches}/4 · ${g.sha256 || '--'})`;
  gp.className = 'pill ' + (g.passed ? 'ok' : 'warn');
  $('syncTimePill').textContent = `同步: ${fmtTime(sync.updated_utc || mon.updated_utc)}`;

  $('kpiGrid').innerHTML = `
    <div class="kpi cyan"><div class="label">已完成批次 / 科学目标</div><div class="value">${k.batches_completed} 批 · ${k.science_targets} 星</div><div class="note">最近完成: ${sync.last_completed_batch || '—'}</div></div>
    <div class="kpi green"><div class="label">盲控已知行星召回</div><div class="value">${k.blind_ok} / ${k.blind_total}</div><div class="note">WASP-18 b & HIP 65 A b 持续校验</div></div>
    <div class="kpi violet"><div class="label">高优候选信号</div><div class="value">${k.high_priority_total}</div><div class="note">周期 ${k.high_priority_periodic} · 单凌星 ${k.high_priority_single}</div></div>
    <div class="kpi amber"><div class="label">深度证伪归档</div><div class="value">${(k.deep_vet_grades?.KEEP_TOP || 0)} KEEP / ${(k.deep_vet_grades?.BORDERLINE || 0)} BDR</div><div class="note">已排除 ${k.deep_vet_grades?.REJECT || 0} · 个案深挖 ${k.deep_dives_count} 例</div></div>
  `;

  // Populate batch selector without resetting user choice
  const sel = $('fBatch');
  const prev = sel.value;
  sel.innerHTML = '<option value="all">全部批次</option>' +
    (data.batches || []).map((b) => `<option value="${b.batch}">${b.batch} (${b.high_priority} 高优)</option>`).join('');
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;

  $('batchGrid').innerHTML = (data.batches || []).map((b) => `
    <div class="batch-card" data-batch="${b.batch}">
      <div class="batch-top">
        <span class="batch-name">${b.batch}</span>
        <span class="badge ${b.blind_pass ? 'b-pass' : 'b-flag'}">盲控 ${b.blind_ok}/${b.blind_total} ${b.blind_pass ? 'PASS' : 'FAIL'}</span>
      </div>
      <div class="batch-stats">
        <div class="batch-stat"><span>科学目标</span><strong>${b.science}</strong></div>
        <div class="batch-stat"><span>高优候选</span><strong>${b.high_priority}</strong></div>
        <div class="batch-stat"><span>四联图</span><strong>${b.png_count}</strong></div>
      </div>
      <div class="blind-line">周期高优 ${b.hp_periodic} · 单凌星高优 ${b.hp_single} · 更新 ${fmtTime(b.updated_utc)}</div>
    </div>
  `).join('');

  document.querySelectorAll('.batch-card').forEach((card) => {
    card.addEventListener('click', () => {
      $('fBatch').value = card.dataset.batch;
      $('fHp').value = 'all';
      switchTab('candidates');
    });
  });
}

async function loadCandidates() {
  const q = new URLSearchParams({
    batch: $('fBatch').value,
    hp: $('fHp').value,
    kind: $('fKind').value,
    catalog: $('fCatalog').value,
    triage: $('fTriage').value,
    min_snr: $('fMinSnr').value || '0',
    min_sde: $('fMinSde').value || '0',
    q: $('fSearch').value.trim(),
  });
  const data = await fetchJson(`/api/candidates?${q.toString()}`);
  state.candidates = data.items || [];
  renderCandidates();
}

function renderCandidates() {
  const rows = [...state.candidates];
  const k = state.sortKey;
  const dir = state.sortDir;
  rows.sort((a, b) => ((a[k] ?? -1e9) - (b[k] ?? -1e9)) * dir);
  state.candidates = rows;
  $('candCount').textContent = `${rows.length} 条`;

  const vetKeys = ['odd_even', 'secondary', 'shape', 'kepler_duration', 'harmonics', 'sap_pdcsap', 'pixel'];
  $('candTbody').innerHTML = rows.map((c, idx) => {
    const dots = vetKeys.map((vk) => {
      const st = c.vet?.[vk]?.state || 'NOT_APPLICABLE';
      return `<span class="vet-dot ${st}" title="${vk}: ${st}"></span>`;
    }).join('');
    const tStatus = c.triage?.status || 'unreviewed';
    return `
      <tr data-idx="${idx}">
        <td>${c.batch.replace('v21_', '')}</td>
        <td><strong style="color:var(--cyan)">${c.tic}</strong>${c.high_priority ? ' <span class="badge b-pass">HP</span>' : ''}</td>
        <td><span class="badge ${c.kind === 'single' ? 'b-violet' : 'b-cyan'}">${c.kind}</span></td>
        <td>${c.period_days ? fmt(c.period_days, 4) : '单次'}</td>
        <td>${fmt(c.depth_ppm, 0)}</td>
        <td>${fmt(c.snr, 2)}</td>
        <td>${c.sde !== null && c.sde !== undefined ? fmt(c.sde, 2) : '—'}</td>
        <td>${fmt(c.rp_rearth, 2)}</td>
        <td>${fmt(c.gaia_ruwe, 2)}</td>
        <td><div class="vet-mini">${dots}</div></td>
        <td>${c.diagnostic_url ? '🖼' : '—'}</td>
        <td><span class="badge ${triageClass(tStatus)}">${triageLabel(tStatus)}</span></td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="12" style="text-align:center;color:var(--muted);padding:28px;">当前筛选条件下无匹配候选体</td></tr>';

  document.querySelectorAll('#candTbody tr[data-idx]').forEach((tr) => {
    tr.addEventListener('click', () => openInspector(Number(tr.dataset.idx)));
  });
}

function openInspector(idx) {
  if (idx < 0 || idx >= state.candidates.length) return;
  state.activeIndex = idx;
  const c = state.candidates[idx];
  $('insTitle').textContent = `TIC ${c.tic} · ${c.batch}`;
  $('insMeta').textContent = `${c.kind === 'periodic' ? `P = ${fmt(c.period_days, 5)} d` : '孤立单凌星'} · 深度 ${fmt(c.depth_ppm, 0)} ppm · SNR ${fmt(c.snr, 2)} · SDE ${c.sde ? fmt(c.sde, 2) : '—'} · 扇区 [${(c.sectors || []).join(', ')}] · 目录 ${c.catalog_state}`;

  const img = $('insImg');
  const noImg = $('insNoImg');
  img.classList.remove('zoomed');
  if (c.diagnostic_url) {
    img.src = c.diagnostic_url;
    img.classList.remove('hidden');
    noImg.classList.add('hidden');
  } else {
    img.removeAttribute('src');
    img.classList.add('hidden');
    noImg.classList.remove('hidden');
  }

  state.triageStatus = c.triage?.status || 'unreviewed';
  $('triageNote').value = c.triage?.note || '';
  document.querySelectorAll('.t-btn').forEach((b) => b.classList.toggle('active', b.dataset.status === state.triageStatus));

  const vetOrder = ['odd_even', 'secondary', 'shape', 'kepler_duration', 'harmonics', 'sap_pdcsap', 'stellar_radius_ruwe', 'pixel', 'catalog', 'flare_window'];
  $('insVetGrid').innerHTML = vetOrder.filter((k) => c.vet?.[k]).map((k) => {
    const v = c.vet[k];
    const st = v.state || '—';
    const cls = st === 'PASS' ? 'b-pass' : st === 'FLAG' ? 'b-flag' : st === 'INCONCLUSIVE' ? 'b-inc' : 'b-na';
    const extra = Object.entries(v).filter(([ek]) => ek !== 'state' && ek !== 'product' && ek !== 'alternative_power' && ek !== 'removed_modes')
      .slice(0, 2).map(([ek, ev]) => `${ek}=${typeof ev === 'number' ? fmt(ev, 2) : JSON.stringify(ev)}`).join(' · ');
    return `<div class="vet-item"><div class="vk">${k}</div><div class="vv"><span class="badge ${cls}">${st}</span> ${extra}</div></div>`;
  }).join('');

  $('insRawJson').textContent = JSON.stringify(c, null, 2);
  $('inspector').classList.remove('hidden');
}

function closeInspector() {
  $('inspector').classList.add('hidden');
  state.activeIndex = -1;
}

async function saveTriage() {
  if (state.activeIndex < 0) return;
  const c = state.candidates[state.activeIndex];
  const res = await fetchJson('/api/triage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: c.key, status: state.triageStatus, note: $('triageNote').value }),
  });
  c.triage = res.entry;
  renderCandidates();
  toast(`已保存 TIC ${c.tic} 的复核标记`);
}

async function loadDeepVet() {
  const d = await fetchJson('/api/deep-vet');
  const g = d.grade_counts || {};
  const r = d.reason_counts || {};
  const topReasons = Object.entries(r).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(' · ');

  $('dvKpiGrid').innerHTML = `
    <div class="kpi green"><div class="label">KEEP_TOP</div><div class="value">${g.KEEP_TOP || 0}</div><div class="note">通过全部深验门槛</div></div>
    <div class="kpi amber"><div class="label">BORDERLINE</div><div class="value">${g.BORDERLINE || 0}</div><div class="note">平滑轮廓未完全排除</div></div>
    <div class="kpi"><div class="label">REJECT</div><div class="value" style="color:var(--red)">${g.REJECT || 0}</div><div class="note">${topReasons || '—'}</div></div>
    <div class="kpi cyan"><div class="label">个案深挖归档</div><div class="value">${(d.deep_dives || []).length}</div><div class="note">含四联诊断图与完整报告</div></div>
  `;

  $('deepDiveList').innerHTML = (d.deep_dives || []).map((dd) => `
    <div class="dd-card">
      <div>
        <h3 style="margin:0 0 8px;color:var(--cyan);font-family:var(--mono)">TIC ${dd.tic} · <span class="badge b-flag">${dd.verdict}</span></h3>
        ${dd.diagnostic_url ? `<img src="${dd.diagnostic_url}" alt="TIC ${dd.tic} diagnostic" onclick="window.open('${dd.diagnostic_url}','_blank')">` : ''}
      </div>
      <div class="dd-report">${(dd.report_markdown || '无报告内容').replace(/[&<>]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m]))}</div>
    </div>
  `).join('') || '<p class="hint">暂无个案深挖归档</p>';

  $('dvTbody').innerHTML = (d.rows || []).map((row) => {
    const gCls = row.final_grade === 'KEEP_TOP' ? 'b-pass' : row.final_grade === 'BORDERLINE' ? 'b-inc' : 'b-flag';
    return `
      <tr>
        <td>${row.id}</td>
        <td>${(row.batch || '').replace('v21_', '')}</td>
        <td style="color:var(--cyan)">${row.tic}</td>
        <td>${fmt(row.period_days, 4)}</td>
        <td>${fmt(row.depth_ppm, 0)}</td>
        <td>${fmt(row.snr, 1)}</td>
        <td>${fmt(row.clean_snr, 2)}</td>
        <td>${fmt(row.kepler_ratio, 2)}</td>
        <td>${fmt(row.u_vs_smooth_bic, 1)}</td>
        <td><span class="badge ${gCls}">${row.final_grade}</span></td>
        <td>${row.first_reason}</td>
      </tr>
    `;
  }).join('');
}

function bindEvents() {
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $('refreshBtn').addEventListener('click', async () => {
    await loadOverview();
    if ($('tab-candidates').classList.contains('active')) await loadCandidates();
    if ($('tab-deepvet').classList.contains('active')) await loadDeepVet();
    toast('已刷新最新本地数据');
  });

  ['fBatch', 'fHp', 'fKind', 'fCatalog', 'fTriage', 'fMinSnr', 'fMinSde', 'fSearch'].forEach((id) => {
    $(id).addEventListener('input', loadCandidates);
    $(id).addEventListener('change', loadCandidates);
  });

  document.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      state.sortDir = state.sortKey === k ? -state.sortDir : -1;
      state.sortKey = k;
      renderCandidates();
    });
  });

  $('closeInsBtn').addEventListener('click', closeInspector);
  $('modalBackdrop').addEventListener('click', closeInspector);
  $('prevCandBtn').addEventListener('click', () => openInspector(state.activeIndex - 1));
  $('nextCandBtn').addEventListener('click', () => openInspector(state.activeIndex + 1));
  $('insImg').addEventListener('click', () => $('insImg').classList.toggle('zoomed'));
  $('saveTriageBtn').addEventListener('click', saveTriage);

  document.querySelectorAll('.t-btn').forEach((b) => {
    b.addEventListener('click', () => {
      state.triageStatus = b.dataset.status;
      document.querySelectorAll('.t-btn').forEach((x) => x.classList.toggle('active', x === b));
    });
  });

  window.addEventListener('keydown', (e) => {
    if ($('inspector').classList.contains('hidden')) return;
    if (e.key === 'Escape') closeInspector();
    if (e.key === 'ArrowUp' && document.activeElement !== $('triageNote')) { e.preventDefault(); openInspector(state.activeIndex - 1); }
    if (e.key === 'ArrowDown' && document.activeElement !== $('triageNote')) { e.preventDefault(); openInspector(state.activeIndex + 1); }
  });
}

(async function init() {
  bindEvents();
  await loadOverview();
  setInterval(() => loadOverview().catch(() => {}), 15000);
})();
