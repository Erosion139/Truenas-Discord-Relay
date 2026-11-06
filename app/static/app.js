"use strict";
// Discord Relay web UI — no framework, no build step.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { status: null, guilds: [], rules: [], channels: new Map(), roles: new Map(), editing: null, labels: {} };

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Requested-With": "relay-ui" }, credentials: "same-origin" };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && !path.startsWith("/api/auth")) { showAuth(); throw new Error("Not logged in"); }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 3000);
}

// ---------------------------------------------------------------- auth
let authMode = "login";
async function boot() {
  const s = await api("/api/auth/state");
  if (s.logged_in) return startApp();
  showAuth(s);
}
async function showAuth(s) {
  s = s || await api("/api/auth/state");
  $("#app").classList.add("hidden"); $("#auth").classList.remove("hidden");
  authMode = s.password_set ? "login" : "setup";
  $("#auth-title").textContent = authMode === "setup" ? "Create a password" : "Sign in";
  $("#auth-help").textContent = authMode === "setup" ? "First run: choose a password that protects this web UI (8+ characters)." : "";
  $("#auth-submit").textContent = authMode === "setup" ? "Create & continue" : "Sign in";
  $("#auth-password2").classList.toggle("hidden", authMode !== "setup");
  $("#auth-password").focus();
}
$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("#auth-password").value;
  $("#auth-error").textContent = "";
  try {
    if (authMode === "setup") {
      if (pw !== $("#auth-password2").value) throw new Error("Passwords don't match");
      await api("/api/auth/setup", { method: "POST", body: { password: pw } });
    } else {
      await api("/api/auth/login", { method: "POST", body: { password: pw } });
    }
    $("#auth-password").value = ""; $("#auth-password2").value = "";
    startApp();
  } catch (err) { $("#auth-error").textContent = err.message; }
});

// ---------------------------------------------------------------- shell
let pollTimer;
async function startApp() {
  $("#auth").classList.add("hidden"); $("#app").classList.remove("hidden");
  await refreshStatus();
  await Promise.all([loadGuilds(), loadRules()]);
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const before = state.status && state.status.status;
    await refreshStatus();
    if (before !== "ready" && state.status.status === "ready") { await loadGuilds(); renderRules(); }
    if (!$("#tab-activity").classList.contains("hidden")) loadActivity();
  }, 5000);
}

$$("#tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  if (name === "activity") loadActivity();
  if (name === "settings") renderSettings();
}

// ---------------------------------------------------------------- status / dashboard
const STATUS_LABEL = { ready: "Online", connecting: "Connecting…", reconnecting: "Reconnecting…", stopped: "Not running", error: "Error" };
async function refreshStatus() {
  try { state.status = await api("/api/status"); } catch { return; }
  const s = state.status;
  $("#status-pill .led").className = "led " + s.status;
  $("#status-text").textContent = STATUS_LABEL[s.status] || s.status;
  $("#d-bot").textContent = s.user ? s.user.name : STATUS_LABEL[s.status];
  $("#d-bot-sub").textContent = s.status === "ready" ? `${STATUS_LABEL.ready}${s.latency_ms != null ? ` · ${s.latency_ms} ms` : ""}` : (s.error || "");
  $("#d-guilds").textContent = s.status === "ready" ? s.guild_count : "—";
  $("#d-rules").textContent = s.rule_count;
  $("#d-rules-sub").textContent = `${state.rules.filter((r) => r.enabled).length} enabled`;
  $("#invite-url").value = s.invite_url || "";
  const open = $("#open-invite");
  if (s.invite_url) open.href = s.invite_url; else open.removeAttribute("href");

  const banner = $("#setup-banner");
  if (!s.token_set) {
    banner.className = "banner";
    banner.innerHTML = `<b>Almost there.</b> Add your bot token to connect:<ol>
      <li>Discord Developer Portal → New Application → <b>Bot</b> → <b>Reset Token</b> and copy it.</li>
      <li>On the same page, turn on <b>Message Content Intent</b>.</li>
      <li>Paste the token in <a href="#" data-goto="settings">Settings</a>.</li></ol>`;
  } else if (s.status === "error") {
    banner.className = "banner err";
    banner.innerHTML = `<b>The bot could not connect.</b> ${esc(s.error || "")}`;
  } else if (s.status === "ready" && s.guild_count === 0) {
    banner.className = "banner";
    banner.innerHTML = "<b>Connected!</b> Now use the invite link below to add the bot to your servers.";
  } else banner.className = "banner hidden";
  $$("[data-goto]", banner).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); switchTab(a.dataset.goto); }));
}

$("#copy-invite").addEventListener("click", async () => {
  const v = $("#invite-url").value;
  if (!v) return toast("Connect the bot first", true);
  try { await navigator.clipboard.writeText(v); toast("Invite link copied"); }
  catch { $("#invite-url").select(); document.execCommand("copy"); toast("Invite link copied"); }
});
$("#refresh-guilds").addEventListener("click", async () => { await loadGuilds(); toast("Refreshed"); });

async function loadGuilds() {
  state.channels.clear(); state.roles.clear();
  try { state.guilds = await api("/api/guilds"); } catch { state.guilds = []; }
  for (const g of state.guilds) {
    for (const c of g.channels) state.channels.set(c.id, { ...c, guild: g });
    for (const r of g.roles) state.roles.set(r.id, { ...r, guild: g });
  }
  const box = $("#guild-list");
  if (!state.guilds.length) {
    box.innerHTML = `<p class="muted">${state.status && state.status.status === "ready" ? "The bot isn't in any server yet — use the invite link above." : "Connect the bot to see servers."}</p>`;
    return;
  }
  box.innerHTML = state.guilds.map((g) => `
    <div class="guild">
      ${g.icon ? `<img src="${esc(g.icon)}" alt="">` : `<div class="ph">${esc(g.name[0] || "?")}</div>`}
      <div class="meta"><b>${esc(g.name)}</b><small>${g.channels.length} text/announcement channels · ID ${esc(g.id)}</small></div>
      <button class="btn ghost small danger" data-leave="${esc(g.id)}">Leave</button>
    </div>`).join("");
  $$("[data-leave]", box).forEach((b) => b.addEventListener("click", async () => {
    const g = state.guilds.find((x) => x.id === b.dataset.leave);
    if (!confirm(`Make the bot leave "${g.name}"? You'll need the invite link to add it back.`)) return;
    try { await api(`/api/guilds/${g.id}/leave`, { method: "POST" }); toast(`Left ${g.name}`); loadGuilds(); }
    catch (err) { toast(err.message, true); }
  }));
}

// ---------------------------------------------------------------- rules list
async function loadRules() {
  state.rules = await api("/api/rules");
  renderRules();
}
function chanLabel(id) {
  const c = state.channels.get(id);
  return c ? `<span class="g">${esc(c.guild.name)} ›</span> #${esc(c.name)}` : `<span class="g">unknown</span> ${esc(id)}`;
}
function renderRules() {
  const box = $("#rule-list");
  if (!state.rules.length) {
    box.innerHTML = `<div class="empty">No relays yet.<br><br><button class="btn primary" id="empty-new">Create your first relay</button></div>`;
    $("#empty-new").addEventListener("click", () => openEditor(null));
    return;
  }
  box.innerHTML = state.rules.map((r) => {
    const who = [];
    if (r.author_user_ids.length) who.push(`${r.author_user_ids.length} ${r.author_user_ids.length === 1 ? "person" : "people"}: ${r.author_user_ids.map((id) => esc(r.labels[id] || id)).join(", ")}`);
    if (r.author_role_ids.length) who.push(`roles: ${r.author_role_ids.map((id) => esc(state.roles.get(id)?.name || id)).join(", ")}`);
    if (!who.length) who.push(r.include_bots ? "everyone incl. bots" : "everyone (no bots)");
    if (r.keywords_include.length) who.push(`must contain: ${r.keywords_include.map(esc).join(" / ")}`);
    if (r.keywords_exclude.length) who.push(`skip: ${r.keywords_exclude.map(esc).join(" / ")}`);
    const modeName = { webhook: "Mirror", embed: "Card", plain: "Plain" }[r.mode];
    return `<div class="rule ${r.enabled ? "" : "off"}">
      <h3>${esc(r.name)} <span class="tag ${r.enabled ? "on" : ""}">${r.enabled ? "on" : "off"}</span> <span class="tag">${modeName}</span></h3>
      <div class="flow">${r.source_channel_ids.map((id) => `<span class="chip">${chanLabel(id)}</span>`).join("")}
        <span class="arrow">→</span>${r.destination_channel_ids.map((id) => `<span class="chip">${chanLabel(id)}</span>`).join("")}</div>
      <div class="filters">From ${who.join(" · ")}</div>
      <div class="actions">
        <button class="btn ghost small" data-test="${esc(r.id)}">Send test</button>
        <button class="btn ghost small" data-toggle="${esc(r.id)}">${r.enabled ? "Disable" : "Enable"}</button>
        <button class="btn small" data-edit="${esc(r.id)}">Edit</button>
      </div></div>`;
  }).join("");
  $$("[data-edit]", box).forEach((b) => b.addEventListener("click", () => openEditor(state.rules.find((r) => r.id === b.dataset.edit))));
  $$("[data-toggle]", box).forEach((b) => b.addEventListener("click", async () => {
    const r = state.rules.find((x) => x.id === b.dataset.toggle);
    try { await api(`/api/rules/${r.id}`, { method: "PUT", body: { ...r, enabled: !r.enabled } }); await loadRules(); refreshStatus(); }
    catch (err) { toast(err.message, true); }
  }));
  $$("[data-test]", box).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const res = await api(`/api/rules/${b.dataset.test}/test`, { method: "POST" });
      const bad = res.filter((x) => !x.ok);
      if (bad.length) toast(bad.map((x) => `${state.channels.get(x.channel_id)?.name || x.channel_id}: ${x.message}`).join(" | "), true);
      else toast(`Test sent to ${res.length} channel(s)`);
    } catch (err) { toast(err.message, true); }
    b.disabled = false;
  }));
}
$("#new-rule").addEventListener("click", () => openEditor(null));

// ---------------------------------------------------------------- pickers
const pickers = {};
function channelOptions() {
  return state.guilds.map((g) => `<optgroup label="${esc(g.name)}">${g.channels.map((c) =>
    `<option value="${esc(c.id)}">#${esc(c.name)}${c.news ? " (announcement)" : ""}${c.category ? ` — ${esc(c.category)}` : ""}</option>`).join("")}</optgroup>`).join("");
}
function roleOptions(guildIds) {
  const gs = guildIds && guildIds.length ? state.guilds.filter((g) => guildIds.includes(g.id)) : state.guilds;
  return gs.map((g) => `<optgroup label="${esc(g.name)}">${g.roles.map((r) => `<option value="${esc(r.id)}">@${esc(r.name)}</option>`).join("")}</optgroup>`).join("");
}
function guildsOf(channelIds) { return [...new Set(channelIds.map((id) => state.channels.get(id)?.guild.id).filter(Boolean))]; }

function makePicker(root) {
  const key = root.dataset.picker, kind = root.dataset.kind;
  const p = { key, kind, values: [] };
  const empty = { channel: "None selected", user: "Anyone", role: "No role filter" }[kind];
  root.innerHTML = `<div class="chips" data-empty="${empty}"></div>
    <div class="add">${kind === "user" ? "" : `<select><option value="">Choose a ${kind}…</option></select>`}
    <input placeholder="${kind === "user" ? "Paste a user ID (or @mention)" : "…or paste an ID"}" style="max-width:${kind === "user" ? "100%" : "220px"}">
    <button type="button" class="btn small">Add</button></div>`;
  const chips = $(".chips", root), sel = $("select", root), input = $("input", root);
  p.render = () => {
    chips.innerHTML = p.values.map((id) => {
      let label = esc(id), warn = false;
      if (kind === "channel") { label = chanLabel(id); warn = !state.channels.has(id); }
      if (kind === "role") { const r = state.roles.get(id); label = r ? `<span class="g">${esc(r.guild.name)} ›</span> @${esc(r.name)}` : esc(id); }
      if (kind === "user") label = state.labels[id] ? `${esc(state.labels[id])} <span class="g">${esc(id)}</span>` : esc(id);
      return `<span class="chip ${warn ? "warn" : ""}">${label}<button type="button" class="x" data-id="${esc(id)}" title="Remove">×</button></span>`;
    }).join("");
    $$(".x", chips).forEach((x) => x.addEventListener("click", () => { p.values = p.values.filter((v) => v !== x.dataset.id); p.render(); onPickerChange(); }));
  };
  p.refreshOptions = () => {
    if (!sel) return;
    const opts = kind === "channel" ? channelOptions() : roleOptions(guildsOf(pickers.source_channel_ids?.values || []));
    sel.innerHTML = `<option value="">Choose a ${kind}…</option>` + opts;
  };
  p.add = (id, label) => {
    id = String(id).trim().replace(/^<[@#][!&]?(\d+)>$/, "$1");
    if (!/^\d{15,21}$/.test(id)) { toast("That's not a valid Discord ID (right-click → Copy ID with Developer Mode on)", true); return; }
    if (label) state.labels[id] = label;
    if (!p.values.includes(id)) p.values.push(id);
    p.render(); onPickerChange();
  };
  $("button.btn", root).addEventListener("click", () => {
    if (input.value.trim()) { p.add(input.value); input.value = ""; }
    else if (sel && sel.value) { p.add(sel.value); sel.value = ""; }
  });
  if (sel) sel.addEventListener("change", () => { if (sel.value) { p.add(sel.value); sel.value = ""; } });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("button.btn", root).click(); } });
  pickers[key] = p;
  return p;
}
$$("[data-picker]").forEach(makePicker);

function onPickerChange() {
  pickers.author_role_ids.refreshOptions();
  // ping role choices come from destination servers
  const sel = $("select[name=ping_role_id]"), cur = sel.value || (state.editing && state.editing.ping_role_id) || "";
  sel.innerHTML = `<option value="">No ping</option>` + roleOptions(guildsOf(pickers.destination_channel_ids.values));
  sel.value = cur;
  if (cur && sel.value !== cur) { sel.insertAdjacentHTML("beforeend", `<option value="${esc(cur)}">${esc(cur)}</option>`); sel.value = cur; }
  destWarnings();
}

function destWarnings() {
  const mode = $("input[name=mode]:checked").value, out = [];
  for (const id of pickers.destination_channel_ids.values) {
    const c = state.channels.get(id); if (!c) continue;
    const miss = [];
    if (!c.perms.view) miss.push("View Channel");
    if (mode === "webhook" && !c.perms.webhooks) miss.push("Manage Webhooks");
    if (mode !== "webhook" && !c.perms.send) miss.push("Send Messages");
    if (mode === "embed" && !c.perms.embed) miss.push("Embed Links");
    if (!c.perms.attach) miss.push("Attach Files");
    if ($("input[name=auto_publish]").checked && c.news && !c.perms.manage_messages) miss.push("Manage Messages (to publish)");
    if (miss.length) out.push(`⚠ #${esc(c.name)} in ${esc(c.guild.name)}: bot is missing ${miss.join(", ")}`);
  }
  for (const id of pickers.source_channel_ids.values) {
    const c = state.channels.get(id); if (!c) continue;
    if (!c.perms.view || !c.perms.read_history) out.push(`⚠ Source #${esc(c.name)}: bot can't read this channel`);
  }
  $("#dest-warnings").innerHTML = out.join("<br>");
}

// ---------------------------------------------------------------- editor
const dlg = $("#rule-dialog"), form = $("#rule-form");
const DEFAULTS = { name: "", enabled: true, source_channel_ids: [], destination_channel_ids: [], author_user_ids: [], author_role_ids: [],
  include_bots: false, keywords_include: [], keywords_exclude: [], mode: "webhook", show_source_in_name: true, prefix_text: "",
  ping_role_id: "", clean_mentions: true, copy_attachments: true, sync_edits: true, sync_deletes: true, auto_publish: false, labels: {} };

function openEditor(rule) {
  state.editing = rule ? { ...rule } : null;
  const r = { ...DEFAULTS, ...(rule || {}) };
  state.labels = { ...r.labels };
  $("#rule-dialog-title").textContent = rule ? "Edit relay" : "New relay";
  $("#delete-rule").classList.toggle("hidden", !rule);
  $("#rule-error").textContent = "";
  $("#author-suggestions").innerHTML = "";
  form.elements.name.value = r.name;
  for (const k of ["enabled", "include_bots", "show_source_in_name", "clean_mentions", "copy_attachments", "sync_edits", "sync_deletes", "auto_publish"]) form.elements[k].checked = !!r[k];
  form.elements.keywords_include.value = r.keywords_include.join("\n");
  form.elements.keywords_exclude.value = r.keywords_exclude.join("\n");
  form.elements.prefix_text.value = r.prefix_text;
  $(`input[name=mode][value=${r.mode}]`).checked = true;
  for (const k of ["source_channel_ids", "destination_channel_ids", "author_user_ids", "author_role_ids"]) {
    pickers[k].values = [...r[k]]; pickers[k].refreshOptions(); pickers[k].render();
  }
  $("select[name=ping_role_id]").value = "";
  onPickerChange();
  $("select[name=ping_role_id]").value = r.ping_role_id;
  onPickerChange();
  modeChanged();
  dlg.showModal();
}
function modeChanged() { $("#show-source-wrap").classList.toggle("hidden", $("input[name=mode]:checked").value !== "webhook"); destWarnings(); }
$$("input[name=mode]").forEach((i) => i.addEventListener("change", modeChanged));
$("input[name=auto_publish]").addEventListener("change", destWarnings);
$("#cancel-rule").addEventListener("click", () => dlg.close());

$("#load-authors").addEventListener("click", async () => {
  const src = pickers.source_channel_ids.values[0];
  const box = $("#author-suggestions");
  if (!src) return toast("Pick a source channel first", true);
  box.innerHTML = `<span class="muted">Loading…</span>`;
  try {
    const authors = await api(`/api/channels/${src}/authors`);
    if (!authors.length) { box.innerHTML = `<span class="muted">No recent messages in that channel.</span>`; return; }
    box.innerHTML = authors.map((a) => `<button type="button" data-id="${esc(a.id)}" data-name="${esc(a.display_name)}">
      <img src="${esc(a.avatar)}" alt=""> ${esc(a.display_name)} <small>${a.bot ? "bot · " : ""}${a.count} msg</small></button>`).join("");
    $$("button", box).forEach((b) => b.addEventListener("click", () => { pickers.author_user_ids.add(b.dataset.id, b.dataset.name); b.remove(); }));
  } catch (err) { box.innerHTML = `<span class="error">${esc(err.message)}</span>`; }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements;
  const body = {
    name: f.name.value, enabled: f.enabled.checked, mode: $("input[name=mode]:checked").value,
    source_channel_ids: pickers.source_channel_ids.values, destination_channel_ids: pickers.destination_channel_ids.values,
    author_user_ids: pickers.author_user_ids.values, author_role_ids: pickers.author_role_ids.values,
    include_bots: f.include_bots.checked, keywords_include: f.keywords_include.value, keywords_exclude: f.keywords_exclude.value,
    show_source_in_name: f.show_source_in_name.checked, prefix_text: f.prefix_text.value, ping_role_id: f.ping_role_id.value,
    clean_mentions: f.clean_mentions.checked, copy_attachments: f.copy_attachments.checked, sync_edits: f.sync_edits.checked,
    sync_deletes: f.sync_deletes.checked, auto_publish: f.auto_publish.checked, labels: state.labels,
  };
  try {
    if (state.editing) await api(`/api/rules/${state.editing.id}`, { method: "PUT", body });
    else await api("/api/rules", { method: "POST", body });
    dlg.close(); toast("Relay saved"); await loadRules(); refreshStatus();
  } catch (err) { $("#rule-error").textContent = err.message; }
});

$("#delete-rule").addEventListener("click", async () => {
  if (!state.editing || !confirm(`Delete relay "${state.editing.name}"?`)) return;
  try { await api(`/api/rules/${state.editing.id}`, { method: "DELETE" }); dlg.close(); toast("Relay deleted"); await loadRules(); refreshStatus(); }
  catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- activity
$("#show-filtered").addEventListener("change", loadActivity);
async function loadActivity() {
  let items = [];
  try { items = await api(`/api/activity?filtered=${$("#show-filtered").checked ? 1 : 0}`); } catch { return; }
  const list = $("#activity-list");
  if (!items.length) { list.innerHTML = `<li class="empty-li">Nothing yet. Post something in a source channel.</li>`; return; }
  list.innerHTML = items.map((i) => {
    const d = new Date(i.ts * 1000);
    const t = d.toLocaleDateString() === new Date().toLocaleDateString() ? d.toLocaleTimeString() : d.toLocaleString();
    return `<li><time>${esc(t)}</time><span class="lvl ${esc(i.level)}">${esc(i.level)}</span>
      <span>${esc(i.message)}${i.jump_url ? ` <a href="${esc(i.jump_url)}" target="_blank" rel="noopener">open</a>` : ""}</span></li>`;
  }).join("");
}

// ---------------------------------------------------------------- settings
async function renderSettings() {
  const s = state.status || {}, a = await api("/api/auth/state");
  $("#token-input").disabled = $("#save-token").disabled = !!s.token_from_env;
  $("#token-input").placeholder = s.token_from_env ? "Set by the DISCORD_TOKEN environment variable" : (s.token_set ? "•••••••• (saved — paste a new one to replace)" : "Bot token");
  $("#pw-row").classList.toggle("hidden", a.password_from_env);
  $("#pw-help").textContent = a.password_from_env ? "The password is set by the WEB_PASSWORD environment variable in your TrueNAS app config." : "Change the password used to open this page.";
}
$("#save-token").addEventListener("click", async () => {
  const token = $("#token-input").value.trim();
  if (!token) return toast("Paste a token first", true);
  try { await api("/api/settings/token", { method: "POST", body: { token } }); $("#token-input").value = ""; toast("Token saved — connecting…"); setTimeout(refreshStatus, 2500); }
  catch (err) { toast(err.message, true); }
});
$("#restart-bot").addEventListener("click", async () => {
  try { await api("/api/bot/restart", { method: "POST" }); toast("Reconnecting…"); setTimeout(async () => { await refreshStatus(); loadGuilds(); }, 3000); }
  catch (err) { toast(err.message, true); }
});
$("#save-pw").addEventListener("click", async () => {
  try { await api("/api/settings/password", { method: "POST", body: { current: $("#pw-current").value, new: $("#pw-new").value } }); $("#pw-current").value = $("#pw-new").value = ""; toast("Password changed"); }
  catch (err) { toast(err.message, true); }
});
$("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); clearInterval(pollTimer); showAuth(); });

boot().catch((err) => toast(err.message, true));
