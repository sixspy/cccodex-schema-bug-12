const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${c.table_name} · v${c.version_no}</h3>
        <span class="chip">${c.status}</span>
      </div>
      <p class="muted">${c.note || ''} · 字段 ${c.field_count}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${c.table_name} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用</button>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      await loadContracts();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card"><strong>${r.as_of}</strong>
        <p>${r.summary}</p>
        <p class="${r.compliance_ok ? 'muted' : 'err'}">合规水线：${r.compliance_detail || '未记录'}</p>
        ${r.compliance_digest ? `<p class="muted">标记摘要 ${r.compliance_digest.slice(0, 12)}</p>` : ''}
        </article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

function chipHtml(chip) {
  if (!chip) return '';
  const cls = chip.stale ? 'chip err' : 'chip';
  return `<span class="${cls}" title="重算摘要 ${chip.digest || '—'}">${chip.label} · 摘要 ${chip.digest_short || '—'}</span>`;
}

function fieldFlagCell(f) {
  return `<label class="flag">
    <input type="checkbox" data-field="${f.field}" ${f.page_masked ? 'checked' : ''} />
    页面脱敏${f.sensitive ? ' · 契约敏感' : ''}
  </label>`;
}

async function renderMask() {
  const d = await api('/api/mask');
  const comp = d.compliance || {};
  const changedNow = (d.chip && d.chip.stale) || !d.chip || !d.chip.digest;
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-refresh" type="button">刷新脱敏</button>
        ${chipHtml(d.chip)}
        <span class="chip">判定：${d.decision}</span>
        ${changedNow ? '<span class="chip err">页面标记待刷新</span>' : ''}
      </div>
      <p class="${comp.ok ? 'muted' : 'err'}">合规水线：${comp.detail || '未取到'}</p>
      ${(comp.problems || []).length ? `<ul class="err">${comp.problems.map(p => `<li>${p}</li>`).join('')}</ul>` : ''}
    </div>
    <div class="card grid">
      <h3>脱敏列表</h3>
      <table class="tbl">
        <thead><tr><th>列</th><th>表</th><th>来源</th><th>出站规则</th><th>页面脱敏</th></tr></thead>
        <tbody>${(d.fields || []).map(f => `
          <tr>
            <td>${f.field}</td>
            <td>${(f.tables || []).join('、') || '—'}</td>
            <td>${f.source}${f.rule_matches ? '' : ' <span class="err">待刷新</span>'}</td>
            <td>${f.rule_masked ? ('已脱敏 ' + (f.sample_masked || '***')) : '明文'}${f.rule_matches ? '' : ` <span class="err">（落库：${f.rule_masked ? '脱敏' : '明文'}）</span>`}</td>
            <td>${fieldFlagCell(f)}</td>
          </tr>`).join('') || '<tr><td colspan="5" class="muted">暂无脱敏列</td></tr>'}</tbody>
      </table>
    </div>
    <div class="card grid">
      <h3>字段笺（契约）</h3>
      <div class="grid">${(d.field_notes || []).map(n => `
        <p class="muted">${n.table_name} v${n.version_no} · ${n.status} · ${n.field_name} ${n.field_type}
        ${n.sensitive ? '<span class="chip">敏感</span>' : ''}</p>`).join('')}</div>
    </div>
    <div class="card grid">
      <h3>出站卡片</h3>
      <div class="row">
        <label>列 <input id="ob-field" value="phone" /></label>
        <label>值 <input id="ob-value" value="13800138000" /></label>
        <button class="primary" id="btn-ob-preview" type="button">预检出站</button>
        <button id="btn-ob-commit" type="button">提交出站</button>
      </div>
      <pre id="ob-out" class="muted"></pre>
      <div class="grid">${(d.outbound_log || []).map(r => `
        <p class="muted">#${r.id} ${r.field} → ${r.payload} ${r.masked ? '（脱敏）' : '（明文）'}
        ${r.quarantined ? ' · 已隔离' : ''} · ${r.note}</p>`).join('') || '<p class="muted">暂无出站记录</p>'}</div>
    </div>
    <div class="card grid">
      <h3>刷新记录</h3>
      <div class="grid">${(d.runs || []).map(r => `
        <p class="muted">#${r.id} ${r.status} · ${r.decision} · 摘要 ${(r.digest || '').slice(0, 12)}
        · 变更 ${r.changed} · ${r.created_at}<br>${r.detail}</p>`).join('') || '<p class="muted">暂无刷新</p>'}</div>
    </div>`;

  document.querySelectorAll('.flag input').forEach(box => {
    box.onchange = async () => {
      try {
        const j = await api('/api/mask/page', {
          method:'POST',
          body: JSON.stringify({field: box.dataset.field, masked: box.checked})
        });
        setMsg(j.message, !box.checked && j.sensitive);
        await renderMask();
      } catch (e) { setMsg(e.message, true); }
    };
  });

  const doOutbound = async (commit) => {
    try {
      const j = await api('/api/outbound', {
        method:'POST',
        body: JSON.stringify({
          field: document.getElementById('ob-field').value.trim(),
          value: document.getElementById('ob-value').value,
          commit,
        })
      });
      document.getElementById('ob-out').textContent = JSON.stringify(j, null, 2);
      setMsg(commit ? j.message : (j.card.masked ? '预检：出站会脱敏' : '预检：会落明文'));
      if (commit) await renderMask();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-ob-preview').onclick = () => doOutbound(false);
  document.getElementById('btn-ob-commit').onclick = () => doOutbound(true);

  document.getElementById('btn-refresh').onclick = async () => {
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    try {
      const j = await api('/api/mask/refresh', {method:'POST', body:'{}'});
      setMsg(j.message);
      await renderMask();
    } catch (e) {
      setMsg(e.message, true);
      await renderMask();
    } finally {
      btn.disabled = false;
    }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'reconcile') await renderReconcile();
  if (tab === 'mask') await renderMask();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
