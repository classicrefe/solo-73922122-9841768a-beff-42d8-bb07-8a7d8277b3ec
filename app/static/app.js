/* 病例切分平台前端（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let currentVersion = null;
let currentState = null;
let lastPreview = null; // {token, version, result, input}

// --------------------------------------------------------------------------- //
// 基础请求
// --------------------------------------------------------------------------- //
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = data?.detail || `请求失败（HTTP ${resp.status}）`;
    const errs = data?.errors ? `：${data.errors.join("；")}` : "";
    throw new Error(msg + errs);
  }
  return data;
}

function setMsg(id, text, ok = false) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "msg " + (ok ? "ok" : "err");
}

function downloadCsv(filename, content) {
  const blob = new Blob(["﻿" + content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 标签页
// --------------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "leak") loadLeakHistory();
  });
});

// --------------------------------------------------------------------------- //
// 模板下载
// --------------------------------------------------------------------------- //
$("tplCases").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("cases_template.csv", "病例号,患者号,标签\nC001,P001,1\nC002,P001,0\nC003,P002,1\n");
});
$("tplRel").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("relatives_template.csv", "患者A,患者B\nP001,P002\n");
});

// --------------------------------------------------------------------------- //
// 上传
// --------------------------------------------------------------------------- //
async function uploadCases() {
  const f = $("casesFile").files[0];
  if (!f) return setMsg("casesMsg", "请先选择病例 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("casesMsg", "上传中…");
  try {
    const r = await api("/api/upload/cases", { method: "POST", body: fd });
    setMsg("casesMsg", `已上传 ${r.case_count} 个病例，新版本 v${r.dataset_version}。${r.notice}`, true);
    $("casesFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("casesMsg", err.message); }
}

async function uploadRelatives() {
  const f = $("relFile").files[0];
  if (!f) return setMsg("relMsg", "请先选择亲属关系 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("relMsg", "上传中…");
  try {
    const r = await api("/api/upload/relatives", { method: "POST", body: fd });
    setMsg("relMsg", `已上传 ${r.pair_count} 对亲属关系，新版本 v${r.dataset_version}`, true);
    $("relFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("relMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 渲染当前数据
// --------------------------------------------------------------------------- //
function renderState() {
  const version = currentState.dataset_version;
  const input = currentState.input;
  currentVersion = version;
  $("currentVersion").textContent = "v" + version;

  const totals = currentState.label_totals;
  $("totL0").textContent = totals[0];
  $("totL1").textContent = totals[1];
  $("t_l0").textContent = totals[0];
  $("t_l1").textContent = totals[1];

  $("overview").innerHTML = [
    ["病例数", input.cases.length],
    ["患者数", currentState.patient_count],
    ["亲属对数", input.relatives.length],
    ["锁定数", Object.keys(input.locks).length],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  $("caseCount").textContent = input.cases.length;
  $("caseTable").querySelector("tbody").innerHTML = input.cases
    .map((c) => `<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.patient_id)}</td><td>${c.label}</td></tr>`)
    .join("");

  $("relCount").textContent = input.relatives.length;
  $("relTable").querySelector("tbody").innerHTML = input.relatives
    .map((r) => `<tr><td>${escapeHtml(r.patient_a)}</td><td>${escapeHtml(r.patient_b)}</td></tr>`)
    .join("");

  // 配额回填（仅当输入框未被聚焦时，避免打断输入）
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const el = $("q_" + key);
    if (document.activeElement !== el) el.value = input.quotas[key] ?? 0;
  }
  renderLocks();
  renderManifestTable();
  renderLeakManifestSelect();
}

// --------------------------------------------------------------------------- //
// 配额
// --------------------------------------------------------------------------- //
async function saveQuotas() {
  const payload = {};
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const v = Number($("q_" + key).value);
    if (!Number.isInteger(v) || v < 0) {
      return setMsg("quotaMsg", `配额 ${key} 必须是非负整数`);
    }
    payload[key] = v;
  }
  try {
    const r = await api("/api/quotas", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMsg("quotaMsg", `配额已保存，新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("quotaMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 锁定表
// --------------------------------------------------------------------------- //
function renderLocks() {
  if (!currentState) return;
  const input = currentState.input;
  // 轮询重建前保留用户尚未保存的选择，避免被 4 秒刷新冲掉
  const prior = {};
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    prior[sel.dataset.lockCase] = sel.value;
  });
  const filter = $("lockFilter").value.trim().toLowerCase();
  const rows = input.cases
    .filter((c) => !filter || c.case_id.toLowerCase().includes(filter) ||
      c.patient_id.toLowerCase().includes(filter))
    .map((c) => {
      const saved = input.locks[c.case_id];
      const effective = Object.prototype.hasOwnProperty.call(prior, c.case_id)
        ? prior[c.case_id]
        : (saved === undefined ? "" : String(saved));
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td><select data-lock-case="${escapeHtml(c.case_id)}">
          <option value=""${effective === "" ? " selected" : ""}>不锁定</option>
          <option value="1"${effective === "1" ? " selected" : ""}>训练集（组1）</option>
          <option value="2"${effective === "2" ? " selected" : ""}>评估集（组2）</option>
        </select></td></tr>`;
    }).join("");
  $("lockTable").querySelector("tbody").innerHTML = rows;
}

async function saveLocks() {
  const locks = [];
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    if (sel.value !== "") locks.push({ case_id: sel.dataset.lockCase, group: Number(sel.value) });
  });
  try {
    const r = await api("/api/locks", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locks }),
    });
    setMsg("lockMsg", `锁定已保存（${r.lock_count} 项），新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("lockMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 求解
// --------------------------------------------------------------------------- //
async function runSolve() {
  $("solveStatus").textContent = "z3 求解中…";
  $("solveStatus").className = "msg";
  $("satBox").classList.add("hidden");
  $("unsatBox").classList.add("hidden");
  try {
    const r = await api("/api/solve", { method: "POST" });
    lastPreview = {
      token: r.preview_token,
      version: r.dataset_version,
      result: r.result,
      input: r.input_snapshot,
    };
    renderSolveResult();
    $("solveStatus").textContent =
      r.result.status === "sat" ? `求解成功（依据 v${r.dataset_version}）` : "约束无解";
    $("solveStatus").className = "msg " + (r.result.status === "sat" ? "ok" : "err");
  } catch (err) {
    $("solveStatus").textContent = err.message;
    $("solveStatus").className = "msg err";
  }
}

function previewIsStale() {
  return !lastPreview || lastPreview.version !== currentVersion;
}

function renderSolveResult() {
  if (!lastPreview) return;
  const { result } = lastPreview;
  if (result.status === "sat") {
    $("satBox").classList.remove("hidden");
    $("unsatBox").classList.add("hidden");
    const counts = result.stats.counts;
    $("solveStats").innerHTML = [
      ["连通分量数", result.stats.component_count],
      ["约束总数", result.constraint_count],
      ["训练集 标签0/1", `${counts.g1_label0} / ${counts.g1_label1}`],
      ["评估集 标签0/1", `${counts.g2_label0} / ${counts.g2_label1}`],
    ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");
    const stale = previewIsStale();
    $("previewWarn").classList.toggle("hidden", !stale);
    $("publishBtn").disabled = stale;
    renderAssignments();
  } else {
    $("satBox").classList.add("hidden");
    $("unsatBox").classList.remove("hidden");
    $("unsatSummary").textContent = result.summary;
    $("conflictTable").querySelector("tbody").innerHTML = result.conflicts
      .map((c, i) => `<tr>
        <td>${i + 1}</td>
        <td><span class="pill ${c.kind === "quota" ? "dead" : "warn"}">${escapeHtml(c.kind_name)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td><code>${escapeHtml(JSON.stringify(c.detail))}</code></td>
      </tr>`).join("");
  }
}

function renderAssignments() {
  if (!lastPreview || lastPreview.result.status !== "sat") return;
  const grpFilter = $("filterGroup").value;
  const labFilter = $("filterLabel").value;
  const locks = lastPreview.input.locks || {};
  const assigns = lastPreview.result.assignments;
  const rows = lastPreview.input.cases
    .filter((c) => (!grpFilter || String(assigns[c.case_id]) === grpFilter) &&
      (!labFilter || String(c.label) === labFilter))
    .map((c) => {
      const g = assigns[c.case_id];
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td>${g}</td>
        <td>${g === 1 ? "训练集" : "评估集"}</td>
        <td>${c.case_id in locks ? "是 → " + (locks[c.case_id] === 1 ? "训练集" : "评估集") : "否"}</td>
      </tr>`;
    }).join("");
  $("assignTable").querySelector("tbody").innerHTML = rows;
}

// --------------------------------------------------------------------------- //
// 发布
// --------------------------------------------------------------------------- //
async function publishManifest() {
  if (!lastPreview || !lastPreview.token) return;
  setMsg("publishMsg", "发布中…");
  try {
    await api("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_token: lastPreview.token }),
    });
    setMsg("publishMsg", "清单已发布并与输入依据一起持久化", true);
    await refreshAll();
  } catch (err) {
    setMsg("publishMsg", err.message);
    if (String(err.message).includes("失效")) {
      $("publishBtn").disabled = true;
      $("previewWarn").classList.remove("hidden");
    }
  }
}

// --------------------------------------------------------------------------- //
// 已发布清单
// --------------------------------------------------------------------------- //
function renderManifestTable() {
  if (!currentState) return;
  const tbody = $("manifestTable").querySelector("tbody");
  if (!currentState.manifests.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">暂无已发布清单</td></tr>`;
    return;
  }
  tbody.innerHTML = currentState.manifests.map((m) => {
    const status = m.expired
      ? `<span class="pill dead">已永久失效${m.invalidated ? "（输入已变动）" : "（版本不匹配）"}</span>`
      : `<span class="pill ok">有效（v${m.dataset_version}）</span>`;
    return `<tr>
      <td><code>${escapeHtml(m.manifest_id.slice(0, 8))}…</code></td>
      <td>v${m.dataset_version}</td>
      <td>${escapeHtml(m.created_at)}</td>
      <td>${m.case_count ?? ""}</td>
      <td>${status}</td>
      <td>
        <button onclick="showManifest('${m.manifest_id}')">查看依据</button>
        <button ${m.expired ? "disabled" : ""} onclick="downloadManifest('${m.manifest_id}')">下载 CSV</button>
      </td></tr>`;
  }).join("");
}

async function showManifest(id) {
  try {
    const m = await api(`/api/manifests/${id}`);
    $("manifestDetail").classList.remove("hidden");
    $("manifestJson").textContent = JSON.stringify(
      { manifest_id: m.manifest_id, dataset_version: m.dataset_version,
        created_at: m.created_at, expired: m.expired,
        input_basis: m.input, split_result: m.result }, null, 2);
  } catch (err) { alert(err.message); }
}

async function downloadManifest(id) {
  const resp = await fetch(`/api/manifests/${id}/download`);
  if (!resp.ok) {
    let msg = `下载被拒绝（HTTP ${resp.status}）`;
    try { msg = (await resp.json()).detail || msg; } catch { /* ignore */ }
    alert(msg);
    await refreshAll();
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `split_manifest_${id.slice(0, 8)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 泄漏审查
// --------------------------------------------------------------------------- //
let lastLeakReview = null;

function renderLeakManifestSelect() {
  if (!currentState) return;
  const select = $("leakManifest");
  const prior = select.value;
  const valid = currentState.manifests.filter((m) => !m.expired);
  const options = valid.map((m) =>
    `<option value="${escapeHtml(m.manifest_id)}">${escapeHtml(m.manifest_id.slice(0, 8))}… · v${m.dataset_version} · ${m.case_count ?? ""} 例 · ${escapeHtml(m.created_at)}</option>`
  ).join("");
  select.innerHTML = options ||
    `<option value="">（没有有效清单，请先求解并发布）</option>`;
  if (prior && valid.some((m) => m.manifest_id === prior)) select.value = prior;
  const expiredCount = currentState.manifests.length - valid.length;
  $("leakManifestHint").textContent = expiredCount > 0
    ? `已隐去 ${expiredCount} 份失效清单（不可用于审查）。`
    : "";
  if (document.querySelector('[data-tab="leak"]').classList.contains("active")) {
    loadLeakHistory();
  }
}

async function loadLeakHistory() {
  try {
    const r = await api("/api/leak-reviews");
    const rows = r.reviews.map((entry) => {
      const ok = entry.status === "clean";
      const staleNote = entry.expired
        ? ` <span class="pill warn">输入已变动·历史结论</span>`
        : "";
      return `<tr>
        <td><code>${escapeHtml(entry.review_id.slice(0, 8))}…</code></td>
        <td><code>${escapeHtml(entry.manifest_id.slice(0, 8))}…</code></td>
        <td>v${entry.dataset_version}</td>
        <td>${entry.supplement.cases.length} 例 / ${entry.supplement.relatives.length} 对新增亲属</td>
        <td><span class="pill ${ok ? "ok" : "warn"}">${escapeHtml(entry.status)}</span>
          ${escapeHtml(entry.conclusion)}${staleNote}</td>
        <td>${escapeHtml(entry.created_at)}</td></tr>`;
    }).join("");
    $("leakHistoryTable").querySelector("tbody").innerHTML = rows ||
      `<tr><td colspan="6" class="hint">暂无审查记录</td></tr>`;
  } catch (err) { /* 轮询/切页静默 */ }
}

async function runLeakReview() {
  const manifestId = $("leakManifest").value;
  if (!manifestId) return setMsg("leakMsg", "请先选择一份有效清单");
  const casesFile = $("leakCasesFile").files[0];
  if (!casesFile) return setMsg("leakMsg", "请先选择补充病例 CSV 文件");
  const relFile = $("leakRelFile").files[0];

  const fd = new FormData();
  fd.append("manifest_id", manifestId);
  fd.append("cases_file", casesFile);
  if (relFile) fd.append("relatives_file", relFile);

  setMsg("leakMsg", "审查中…");
  $("leakResultBox").classList.add("hidden");
  try {
    const r = await api("/api/leak-review", { method: "POST", body: fd });
    lastLeakReview = r;
    renderLeakResult(r);
    const res = r.result;
    setMsg(
      "leakMsg",
      res.has_leakage
        ? `审查完成：${res.leakage_case_count} 个补充病例存在跨组泄漏`
        : (res.has_manifest_conflict
          ? "审查完成：补充病例无跨组泄漏，但新增关系造成清单内部冲突"
          : `审查完成：${res.clean_case_count} 个补充病例未发现跨组泄漏`),
      !res.has_leakage && !res.has_manifest_conflict
    );
    loadLeakHistory();
  } catch (err) {
    setMsg("leakMsg", err.message);
  }
}

function renderLeakResult(review) {
  const res = review.result;
  $("leakResultBox").classList.remove("hidden");
  const banner = $("leakConclusion");
  banner.className = "banner " + (
    res.has_leakage ? "" : (res.has_manifest_conflict ? "warn" : "ok-banner")
  );
  banner.textContent = res.conclusion;

  const stats = res.stats;
  $("leakStats").innerHTML = [
    ["补充病例", stats.supplement_case_count],
    ["新增亲属对", stats.new_relative_count],
    ["患者总数", stats.patient_count],
    ["含两组的连通分量", stats.leak_component_count],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  // 受影响的补充病例 + 原始关系路径
  $("leakAffectedBox").classList.toggle("hidden", !res.affected_supplement_cases.length);
  $("leakAffected").innerHTML = res.affected_supplement_cases.map((item) => {
    const paths = item.paths.map((p, i) => `
      <li>
        <div><b>${i + 1}.</b> 通向 ${escapeHtml(p.to_case.origin === "manifest" ? "清单" : "补充")}病例
          <code>${escapeHtml(p.to_case.case_id)}</code>
          （患者 <code>${escapeHtml(p.to_case.patient_id)}</code>，${escapeHtml(p.to_case.group_name)}）
          · ${p.step_count} 步</div>
        <div class="path-text">${escapeHtml(p.text)}</div>
        <div class="rel-chips">${p.relations.map((rel) => rel.kind === "same_patient"
          ? `<span class="chip same">同一患者 ${escapeHtml(rel.patient_id)}</span>`
          : `<span class="chip ${rel.source === "supplement" ? "new" : ""}">${escapeHtml(rel.source_name)} ${escapeHtml(rel.patient_a)}–${escapeHtml(rel.patient_b)}</span>`
        ).join(" → ")}</div>
      </li>`).join("");
    return `<div class="leak-card">
      <h3>补充病例 <code>${escapeHtml(item.case_id)}</code>
        （患者 ${escapeHtml(item.patient_id)}，目标${escapeHtml(item.target_group_name)}）</h3>
      <p class="hint">该患者所在关系分量含 ${item.opposite_case_count} 个${escapeHtml(item.opposite_group_name)}病例
        （清单 ${item.opposite_manifest_case_count} / 补充 ${item.opposite_supplement_case_count}）。
        ${item.paths_truncated ? `仅展示最短的 ${item.paths.length} 条，另有 ${item.omitted_path_count} 条未展示。` : ""}</p>
      <ol class="path-list">${paths}</ol>
    </div>`;
  }).join("");

  // 新增关系造成的清单内部冲突
  $("leakManifestConflictBox").classList.toggle(
    "hidden", !res.manifest_internal_conflicts.length);
  $("leakManifestConflicts").innerHTML = res.manifest_internal_conflicts.map((c) => {
    const cause = c.caused_by_new_relations
      ? `<span class="pill dead">新增关系造成</span> ${c.new_relations.map((r) =>
          `<span class="chip new">${escapeHtml(r.patient_a)}–${escapeHtml(r.patient_b)}</span>`).join(" ")}`
      : `<span class="pill warn">路径未使用新增关系</span>`;
    return `<div class="leak-card">
      <h3>清单病例 <code>${escapeHtml(c.case_a.case_id)}</code>（${escapeHtml(c.case_a.group_name)}）
        ↔ <code>${escapeHtml(c.case_b.case_id)}</code>（${escapeHtml(c.case_b.group_name)}）</h3>
      <div>${cause}</div>
      <p class="hint">该连通分量共含 ${c.manifest_case_count} 个清单病例，
        跨组配对 ${c.conflicting_pair_count} 对，此处仅展示最近的一对及其路径。</p>
      <div class="path-text">${escapeHtml(c.path.text)}</div>
    </div>`;
  }).join("") + (res.manifest_conflicts_truncated
    ? `<p class="hint">冲突分量较多，仅展示前 ${res.manifest_internal_conflicts.length} 个
       （共 ${res.manifest_conflict_component_count} 个）。</p>` : "");

  // 无泄漏的补充病例
  $("leakCleanBox").classList.toggle("hidden", !res.clean_supplement_cases.length);
  $("leakCleanTable").querySelector("tbody").innerHTML =
    res.clean_supplement_cases.map((c) => `<tr>
      <td>${escapeHtml(c.case_id)}</td>
      <td>${escapeHtml(c.patient_id)}</td>
      <td>${escapeHtml(c.target_group_name)}（${c.target_group}）</td></tr>`).join("");
}

// --------------------------------------------------------------------------- //
// 版本日志
// --------------------------------------------------------------------------- //
async function loadVersionLog() {
  try {
    const r = await api("/api/version-log");
    $("logTable").querySelector("tbody").innerHTML = r.log.map((e) => `<tr>
      <td>v${e.dataset_version}</td>
      <td>${escapeHtml(e.change_type)}</td>
      <td><code>${escapeHtml(JSON.stringify(e.detail))}</code></td>
      <td>${escapeHtml(e.created_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="hint">暂无变动记录</td></tr>`;
  } catch (err) { /* 轮询场景静默 */ }
}

// --------------------------------------------------------------------------- //
// 全局刷新与后台轮询（支持两个窗口交错操作）
// --------------------------------------------------------------------------- //
async function refreshAll(showStaleBanner = false) {
  try {
    const prevVersion = currentVersion;
    currentState = await api("/api/state");
    renderState();
    $("healthBadge").textContent = "已连接";
    $("healthBadge").className = "badge";
    const changed = prevVersion !== null && prevVersion !== currentState.dataset_version;
    if (changed || showStaleBanner) {
      $("staleBanner").classList.toggle("hidden", !changed && !showStaleBanner);
      if (lastPreview && lastPreview.version !== currentVersion) {
        $("previewWarn").classList.remove("hidden");
        $("publishBtn").disabled = true;
      }
      if (changed && lastLeakReview) {
        // 输入版本已变：上次审查结论对应旧版本，不再展示为当前结论
        lastLeakReview = null;
        $("leakResultBox").classList.add("hidden");
        setMsg("leakMsg", "输入已变动，上次审查结论已过期，请重新审查。");
      }
    }
    if (lastPreview) renderSolveResult();
  } catch (err) {
    $("healthBadge").textContent = "连接中断";
    $("healthBadge").className = "badge down";
  }
}

setInterval(refreshAll, 4000);
setInterval(() => {
  if ($("tab-log").classList.contains("active")) loadVersionLog();
}, 5000);

refreshAll();
