"use strict";

const $ = (id) => document.getElementById(id);

function pill(state) {
  return `<span class="pill ${state}">${state}</span>`;
}

function alertBox(kind, text) {
  const el = $("form-alert");
  el.className = "alert " + kind;
  el.textContent = text;
}

function clearAlert() {
  const el = $("form-alert");
  el.className = "alert";
  el.textContent = "";
}

function receipts(obj) {
  const names = Object.keys(obj || {});
  if (!names.length) return "—";
  return names.map((n) => {
    const r = obj[n];
    if (!r) return `${n}: 等待中…`;
    return `${n}: digest=${r.digest.slice(0, 16)}… nonce=${r.nonce.slice(0, 8)} @${r.issued_at}`;
  }).join("\n");
}

function render(payload) {
  const r = payload.release;
  $("v-id").textContent = r.release;
  $("v-state").innerHTML = pill(r.state);
  $("v-digest").textContent = r.current_digest || r.sha256 || "—";
  $("v-progress").innerHTML =
    `意图持久化 ✓ | 准备：${(r.progress.prepared || []).join(", ") || "无"} | ` +
    `激活：${(r.progress.activated || []).join(", ") || "无"}`;
  $("v-prepare").textContent = receipts(r.prepare_evidence);
  $("v-active").textContent = receipts(r.activate_evidence);
  $("v-error").textContent = (r.state === "REJECTED" && r.error) ? r.error : "—";
  $("v-raw").textContent = JSON.stringify(r, null, 2);
}

function digestShort(d) {
  return d.length > 20 ? d.slice(0, 20) + "…" : d;
}

async function refreshActive() {
  try {
    const resp = await fetch("/api/active");
    const active = await resp.json();
    $("active-digest").textContent = active.active_digest
      ? `${digestShort(active.active_digest)} (${active.active_release})`
      : "（尚无成功发布）";
  } catch (e) {
    $("active-digest").textContent = "（健康检查失败）";
  }
}

async function queryRelease() {
  const rid = $("rid").value.trim();
  if (!rid) { alertBox("bad", "请输入发布标识后再查询。"); return; }
  clearAlert();
  try {
    const resp = await fetch("/api/releases/" + encodeURIComponent(rid));
    const payload = await resp.json();
    if (payload.release) render(payload);
    if (resp.status === 404) alertBox("bad", payload.error);
    else if (resp.status === 202) alertBox("warn", payload.message);
    else if (resp.status === 200 && payload.ok)
      alertBox("ok", "双仓已激活相同 SHA-256，发布完成。");
  } catch (e) {
    alertBox("bad", "查询失败：" + e.message);
  }
}

async function submitRelease() {
  const rid = $("rid").value.trim();
  const artifact = $("art").value.trim();
  if (!rid) { alertBox("bad", "请输入发布标识。"); return; }
  if (!artifact) { alertBox("bad", "请输入 Base64 工件。"); return; }
  $("submit-btn").disabled = true;
  clearAlert();
  try {
    const resp = await fetch("/api/releases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_id: rid, artifact }),
    });
    const payload = await resp.json();
    if (payload.release) render(payload);
    if (resp.status >= 400) {
      alertBox("bad", payload.error || "提交失败。");
    } else if (payload.pending) {
      alertBox("warn", payload.message);
    } else {
      alertBox("ok", payload.replay
        ? "重复提交（同标识同工件）：回放首次回执，未产生第二次激活。"
        : "双仓激活相同 SHA-256，发布完成！");
    }
  } catch (e) {
    alertBox("bad", "连接控制服务失败：" + e.message);
  } finally {
    $("submit-btn").disabled = false;
    refreshActive();
  }
}

$("query-btn").addEventListener("click", queryRelease);
$("submit-btn").addEventListener("click", submitRelease);
$("rid").addEventListener("keydown", (e) => { if (e.key === "Enter") queryRelease(); });
$("art").addEventListener("keydown", (e) => { if (e.key === "Enter") queryRelease(); });
refreshActive();
