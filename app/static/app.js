/* Networking AI — single-page dashboard (vanilla JS, no build step). */

const API = "/api";
const RELATIONSHIPS = ["family","friend","colleague","client","partner","mentor","investor","acquaintance","other"];
const FREQUENCIES = ["weekly","biweekly","monthly","quarterly","biannual","yearly"];
const CHANNELS = ["call","message","email","meeting","social","event","other"];
const FACT_TYPES = ["role","employer","location","interest","family","relationship","preference","other"];

const state = { view: "dashboard", aiEnabled: false };

/* ── HTTP ──────────────────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ── Helpers ───────────────────────────────────────────────────────────── */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
const initials = (c) => ((c.first_name?.[0] || "") + (c.last_name?.[0] || "")).toUpperCase() || "?";
const titleCase = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  setTimeout(() => (t.className = "toast hidden"), 3200);
}

function warmthBar(c) {
  const pct = Math.round(c.warmth_score);
  return `<div class="warmth">
    <span class="dot bg-${c.warmth_status}"></span>
    <div class="warmth-bar"><div class="warmth-fill bg-${c.warmth_status}" style="width:${pct}%"></div></div>
    <small class="status-${c.warmth_status}">${pct}</small>
  </div>`;
}

function daysLabel(n) {
  if (n === 0) return "today";
  if (n === 1) return "in 1 day";
  return `in ${n} days`;
}

function sourceBadge(source) {
  if (!source || source === "manual") return "";
  const label = source === "gmail" ? "via Gmail" : source === "gcal" ? "via Calendar" : "via " + source;
  return `<span class="pill" style="margin-left:6px">${label}</span>`;
}

/* ── Modal ─────────────────────────────────────────────────────────────── */
function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-host").classList.remove("hidden");
}
function closeModal() { $("#modal-host").classList.add("hidden"); }
$("#modal-host").addEventListener("click", (e) => {
  if (e.target.classList.contains("modal-backdrop")) closeModal();
});

/* ── Router ────────────────────────────────────────────────────────────── */
function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  if (view === "dashboard") renderDashboard();
  else if (view === "tasks") renderTasks();
  else if (view === "inbox") renderInbox();
  else if (view === "contacts") renderContacts();
  else if (view === "goals") renderGoals();
  else if (view === "integrations") renderIntegrations();
}
document.querySelectorAll(".nav-item").forEach((b) =>
  b.addEventListener("click", () => setView(b.dataset.view)));
$("#add-contact-btn").addEventListener("click", () => openContactForm());

/* ── Dashboard ─────────────────────────────────────────────────────────── */
async function renderDashboard() {
  const main = $("#main");
  main.innerHTML = `<div class="view-head"><h2>Dashboard</h2></div><div class="empty"><span class="spinner"></span> Loading…</div>`;
  let d;
  try { d = await api("/dashboard"); } catch (e) { return showError(e); }

  const s = d.stats;
  const stat = (label, value) => `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div></div>`;

  const suggestions = d.suggestions.length
    ? d.suggestions.map((sg) => `
      <div class="suggestion" data-id="${sg.contact.id}">
        <div class="avatar">${initials(sg.contact)}</div>
        <div class="meta" style="flex:1">
          <div class="name">${esc(sg.contact.full_name)}</div>
          <div class="reason">${esc(sg.reason)}</div>
        </div>
        ${warmthBar(sg.contact)}
        <button class="btn small primary" data-rec="${sg.contact.id}">Suggest</button>
      </div>`).join("")
    : `<div class="empty">Nobody is overdue. You're on top of your network. ✦</div>`;

  const upcoming = d.upcoming.length
    ? d.upcoming.map((u) => `
      <div class="suggestion" data-id="${u.contact.id}">
        <div class="avatar">${initials(u.contact)}</div>
        <div class="meta" style="flex:1">
          <div class="name">${esc(u.contact.full_name)}</div>
          <div class="reason">${esc(u.label)} · ${daysLabel(u.days_away)}</div>
        </div>
      </div>`).join("")
    : `<div class="empty muted">No upcoming dates in the next two weeks.</div>`;

  const events = d.pending_events.length
    ? d.pending_events.map((ev) => `
      <div class="suggestion">
        <div class="meta" style="flex:1">
          <div class="name">${esc(ev.title)}</div>
          <div class="reason">${esc(ev.event_type)} · ${esc(ev.source)}</div>
        </div>
      </div>`).join("")
    : `<div class="empty muted">No new life events detected.</div>`;

  main.innerHTML = `
    <div class="view-head">
      <h2>Dashboard</h2>
      <button class="btn ghost" id="refresh-warmth">↻ Recompute warmth</button>
    </div>
    <div class="grid stats-grid">
      ${stat("Contacts", s.total_contacts)}
      ${stat("Due now", s.due_now)}
      ${stat("Avg warmth", s.average_warmth)}
      ${stat("Pending events", s.pending_life_events)}
    </div>
    <div class="two-col">
      <div class="card">
        <div class="flex between mb"><h3 class="section-title">Reach out today</h3></div>
        ${suggestions}
      </div>
      <div>
        <div class="card mb">
          <h3 class="section-title">Upcoming dates</h3>
          ${upcoming}
        </div>
        <div class="card">
          <h3 class="section-title">New life events</h3>
          ${events}
        </div>
      </div>
    </div>`;

  main.querySelectorAll(".suggestion[data-id]").forEach((row) =>
    row.addEventListener("click", (e) => {
      if (e.target.dataset.rec) return;
      openContactDetail(Number(row.dataset.id));
    }));
  main.querySelectorAll("[data-rec]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); showRecommendation(Number(b.dataset.rec)); }));
  $("#refresh-warmth").addEventListener("click", async () => {
    try { await api("/maintenance/refresh-warmth", { method: "POST" }); toast("Warmth recomputed"); renderDashboard(); }
    catch (e) { toast(e.message, true); }
  });
}

/* ── Contacts list ─────────────────────────────────────────────────────── */
async function renderContacts() {
  const main = $("#main");
  main.innerHTML = `
    <div class="view-head"><h2>Contacts</h2>
      <button class="btn primary" id="new-c">+ New contact</button></div>
    <div class="toolbar">
      <input class="search" id="search" placeholder="Search name or company…" />
      <select id="filter-rel"><option value="">All relationships</option>
        ${RELATIONSHIPS.map((r) => `<option value="${r}">${titleCase(r)}</option>`).join("")}</select>
      <select id="sort"><option value="warmth">Coldest first</option>
        <option value="due">Most overdue</option>
        <option value="recent">Recently contacted</option>
        <option value="name">Name</option></select>
    </div>
    <div id="contact-list" class="card"><div class="empty"><span class="spinner"></span></div></div>`;

  $("#new-c").addEventListener("click", () => openContactForm());
  const reload = () => loadContactList();
  $("#search").addEventListener("input", debounce(reload, 250));
  $("#filter-rel").addEventListener("change", reload);
  $("#sort").addEventListener("change", reload);
  loadContactList();
}

async function loadContactList() {
  const params = new URLSearchParams();
  const q = $("#search")?.value.trim();
  const rel = $("#filter-rel")?.value;
  const sort = $("#sort")?.value || "warmth";
  if (q) params.set("search", q);
  if (rel) params.set("relationship", rel);
  params.set("sort", sort);

  const list = $("#contact-list");
  try {
    const contacts = await api("/contacts?" + params.toString());
    if (!contacts.length) { list.innerHTML = `<div class="empty">No contacts yet. Add your first one.</div>`; return; }
    list.innerHTML = contacts.map((c) => `
      <div class="row" data-id="${c.id}">
        <div class="avatar">${initials(c)}</div>
        <div class="meta">
          <div class="name">${esc(c.full_name)}</div>
          <div class="sub">${esc([titleCase(c.relationship_type), c.position, c.company].filter(Boolean).join(" · "))}</div>
        </div>
        ${warmthBar(c)}
      </div>`).join("");
    list.querySelectorAll(".row").forEach((r) =>
      r.addEventListener("click", () => openContactDetail(Number(r.dataset.id))));
  } catch (e) { showError(e); }
}

/* ── Contact detail ────────────────────────────────────────────────────── */
async function openContactDetail(id, tab = "overview") {
  const main = $("#main");
  main.innerHTML = `<div class="empty"><span class="spinner"></span> Loading…</div>`;
  let c;
  try { c = await api(`/contacts/${id}`); } catch (e) { return showError(e); }

  const tabs = ["overview", "facts", "interactions", "events", "social"];
  const tabBtns = tabs.map((t) =>
    `<button class="tab ${t === tab ? "active" : ""}" data-tab="${t}">${titleCase(t)}</button>`).join("");

  main.innerHTML = `
    <button class="btn ghost mb" id="back">← Back</button>
    <div class="detail-head">
      <div class="avatar">${initials(c)}</div>
      <div style="flex:1">
        <div class="flex between">
          <div>
            <h2>${esc(c.full_name)}</h2>
            <div class="role">${esc([c.position, c.company].filter(Boolean).join(" at "))}</div>
          </div>
          <div class="flex">
            <button class="btn" id="edit">Edit</button>
            <button class="btn danger" id="del">Delete</button>
          </div>
        </div>
        <div class="flex mt" style="gap:18px">
          <div style="width:200px">${warmthBar(c)}</div>
          <span class="pill">${titleCase(c.relationship_type)}</span>
          <span class="pill">cadence: ${c.contact_frequency}</span>
          <span class="muted">${c.is_due ? "⚠ due now" : `last contact ${c.days_since_contact}d ago`}</span>
        </div>
        <div class="tags mt">${c.tags.map((t) => `<span class="pill accent">${esc(t)}</span>`).join("")}</div>
        <div class="tags mt">${(c.goals || []).map((g) =>
          `<span class="pill" style="background:#7c5cff22">🎯 ${esc(g.title)}${g.status !== "active" ? " (" + esc(g.status) + ")" : ""} <a data-goalunlink="${g.id}" style="cursor:pointer">✕</a></span>`).join("")}
          <button class="btn small" id="link-goal">🎯 + ціль</button></div>
      </div>
    </div>
    <div class="flex mb" style="gap:8px">
      <button class="btn primary small" id="dossier-btn">✦ ${c.ai_dossier ? "Refresh" : "Generate"} dossier</button>
      <button class="btn small" id="rec-btn">✦ Outreach suggestion</button>
      <button class="btn small" id="log-btn">+ Log interaction</button>
      <button class="btn small" id="import-btn">⬇ Import from link</button>
    </div>
    <div id="ai-area"></div>
    <div class="tabs">${tabBtns}</div>
    <div id="tab-body"></div>`;

  $("#back").addEventListener("click", () => setView(state.lastList || "contacts"));
  $("#edit").addEventListener("click", () => openContactForm(c));
  $("#del").addEventListener("click", () => confirmDelete(c));
  $("#dossier-btn").addEventListener("click", () => generateDossier(c.id));
  $("#rec-btn").addEventListener("click", () => showRecommendation(c.id));
  $("#log-btn").addEventListener("click", () => openInteractionForm(c.id));
  $("#import-btn").addEventListener("click", () => openImportForm(c.id));
  main.querySelectorAll(".tab").forEach((b) =>
    b.addEventListener("click", () => { renderTab(c, b.dataset.tab); main.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b)); }));

  $("#link-goal")?.addEventListener("click", () => linkGoalToContact(c.id));
  main.querySelectorAll("[data-goalunlink]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/goals/${b.dataset.goalunlink}/contacts/${c.id}`, { method: "DELETE" });
      openContactDetail(c.id, tab); } catch (e) { toast(e.message, true); }
  }));

  if (c.ai_dossier) $("#ai-area").innerHTML = dossierHtml(c.ai_dossier);
  renderTab(c, tab);
}

function dossierHtml(text) {
  return `<div class="card mb"><h3 class="section-title">AI dossier</h3><div class="ai-box">${esc(text)}</div></div>`;
}

function renderTab(c, tab) {
  const body = $("#tab-body");
  if (tab === "overview") {
    const f = (k, v) => v ? `<div class="field-row"><span class="k">${k}</span><span>${v}</span></div>` : "";
    const link = (k, v) => v ? `<div class="field-row"><span class="k">${k}</span><a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a></div>` : "";
    body.innerHTML = `<div class="card">
      ${f("Email", esc(c.email))}
      ${f("Phone", esc(c.phone))}
      ${f("WhatsApp", esc(c.whatsapp))}
      ${f("Telegram", esc(c.telegram))}
      ${f("Location", esc(c.location))}
      ${f("Birthday", c.birth_date || "")}
      ${link("LinkedIn", c.linkedin_url)}
      ${link("Instagram", c.instagram_url)}
      ${link("Facebook", c.facebook_url)}
      ${link("Twitter/X", c.twitter_url)}
      ${link("GitHub", c.github_url)}
      ${link("Website", c.website_url)}
      ${c.notes ? `<div class="field-row"><span class="k">Notes</span><span style="max-width:60%;text-align:right">${esc(c.notes)}</span></div>` : ""}
      ${c.key_dates.length ? `<h3 class="section-title mt">Key dates</h3>${c.key_dates.map((k) => `<div class="field-row"><span class="k">${esc(k.label)}</span><span>${k.date}</span></div>`).join("")}` : ""}
    </div>`;
  } else if (tab === "facts") {
    const facts = c.facts || [];
    const rows = facts.map((f) => `
      <div class="timeline-item flex between" style="align-items:flex-start">
        <div>
          <span class="pill accent">${esc(titleCase(f.fact_type))}</span>
          <span>${esc(f.value)}</span>
          <small class="muted"> · ${esc(f.source)}${f.confidence != null ? " · " + Math.round(f.confidence * 100) + "%" : ""}</small>
        </div>
        <button class="btn small danger" data-factdel="${f.id}" title="No longer true">✕</button>
      </div>`).join("");
    body.innerHTML = `
      <div class="flex mb" style="gap:6px">
        <button class="btn primary small" id="extract-facts">✦ Extract facts (AI)</button>
        <button class="btn small" id="add-fact">+ Add fact</button>
      </div>
      ${facts.length ? `<div class="card">${rows}</div>`
        : `<div class="empty">No facts yet. Add one, or let AI extract them from the history.</div>`}`;
    $("#extract-facts").addEventListener("click", () => extractFacts(c.id));
    $("#add-fact").addEventListener("click", () => openFactForm(c.id));
    body.querySelectorAll("[data-factdel]").forEach((b) =>
      b.addEventListener("click", () => invalidateFact(c.id, Number(b.dataset.factdel))));
  } else if (tab === "interactions") {
    body.innerHTML = c.interactions.length
      ? `<div class="card">${c.interactions.map((i) => `
          <div class="timeline-item">
            <div class="flex between">
              <strong>${titleCase(i.channel)} · ${titleCase(i.direction)} ${sourceBadge(i.source)}</strong>
              <span class="muted">${new Date(i.occurred_at).toLocaleDateString()}</span>
            </div>
            <div>${esc(i.summary || "—")}</div>
            <small class="muted">sentiment ${i.sentiment >= 0 ? "+" : ""}${i.sentiment}</small>
          </div>`).join("")}</div>`
      : `<div class="empty">No interactions logged. Use “Log interaction”.</div>`;
  } else if (tab === "events") {
    body.innerHTML = c.life_events.length
      ? `<div class="card">${c.life_events.map((ev) => `
          <div class="timeline-item">
            <div class="flex between">
              <strong>${esc(ev.title)}</strong>
              <span class="pill">${esc(ev.status)}</span>
            </div>
            <div class="muted">${esc(ev.event_type)} · ${esc(ev.source)}${ev.event_date ? " · " + ev.event_date : ""}</div>
            ${ev.description ? `<div>${esc(ev.description)}</div>` : ""}
            ${ev.suggested_message ? `<div class="ai-box mt">${esc(ev.suggested_message)}</div>` : ""}
            <div class="flex mt" style="gap:6px">
              <button class="btn small" data-evmsg="${ev.id}">✦ Draft message</button>
              ${ev.status !== "acted" ? `<button class="btn small" data-evdone="${ev.id}">Mark acted</button>` : ""}
            </div>
          </div>`).join("")}</div>`
      : `<div class="empty">No life events yet. Import a social link to detect them.</div>`;
    body.querySelectorAll("[data-evmsg]").forEach((b) =>
      b.addEventListener("click", () => draftEventMessage(c.id, Number(b.dataset.evmsg))));
    body.querySelectorAll("[data-evdone]").forEach((b) =>
      b.addEventListener("click", () => markEvent(c.id, Number(b.dataset.evdone))));
  } else if (tab === "social") {
    body.innerHTML = c.social_snapshots.length
      ? `<div class="card">${c.social_snapshots.map((s) => `
          <div class="timeline-item">
            <div class="flex between"><strong>${titleCase(s.platform)}</strong>
              <span class="muted">${new Date(s.fetched_at).toLocaleDateString()}</span></div>
            <a href="${esc(s.url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(s.url)}</a>
            ${s.title ? `<div>${esc(s.title)}</div>` : ""}
            ${s.raw_text ? `<div class="muted">${esc(s.raw_text.slice(0, 400))}</div>` : ""}
          </div>`).join("")}</div>`
      : `<div class="empty">No social snapshots. Import a link to capture one.</div>`;
  }
}

/* ── AI actions ────────────────────────────────────────────────────────── */
async function generateDossier(id) {
  const btn = $("#dossier-btn");
  const area = $("#ai-area");
  if (btn) btn.innerHTML = `<span class="spinner"></span> Thinking…`;
  area.innerHTML = `<div class="card mb"><div class="ai-box"><span class="spinner"></span> Generating dossier…</div></div>`;
  try {
    const c = await api(`/contacts/${id}/dossier`, { method: "POST" });
    area.innerHTML = dossierHtml(c.ai_dossier);
    if (btn) btn.innerHTML = "✦ Refresh dossier";
  } catch (e) { toast(e.message, true); area.innerHTML = ""; if (btn) btn.textContent = "✦ Generate dossier"; }
}

async function showRecommendation(id) {
  openModal(`<h3>Outreach suggestion</h3><div class="ai-box"><span class="spinner"></span> Thinking…</div>
    <div class="modal-actions"><button class="btn" id="m-close">Close</button></div>`);
  $("#m-close").addEventListener("click", closeModal);
  try {
    const r = await api(`/contacts/${id}/recommendation`);
    openModal(`
      <h3>Outreach suggestion ${r.source === "ai" ? "" : '<span class="badge off">rule-based</span>'}</h3>
      <div class="rec-grid">
        <div><span class="muted">Should contact</span><br><strong>${r.should_contact ? "Yes" : "Not urgent"}</strong></div>
        <div><span class="muted">Urgency</span><br><strong>${titleCase(r.urgency)}</strong></div>
        <div><span class="muted">Channel</span><br><strong>${titleCase(r.channel)}</strong></div>
        <div><span class="muted">Timing</span><br><strong>${esc(r.timing)}</strong></div>
      </div>
      <p class="muted">${esc(r.reason)}</p>
      ${r.talking_points.length ? `<h4>Talking points</h4><ul>${r.talking_points.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      <h4>Draft message</h4>
      <div class="ai-box">${esc(r.draft_message)}</div>
      <div class="modal-actions">
        <button class="btn" id="m-copy">Copy draft</button>
        <button class="btn primary" id="m-close2">Done</button>
      </div>`);
    $("#m-copy").addEventListener("click", () => { navigator.clipboard?.writeText(r.draft_message); toast("Draft copied"); });
    $("#m-close2").addEventListener("click", closeModal);
  } catch (e) { toast(e.message, true); closeModal(); }
}

async function draftEventMessage(contactId, eventId) {
  try {
    const r = await api(`/life-events/${eventId}/message`, { method: "POST" });
    toast("Message drafted");
    openContactDetail(contactId, "events");
  } catch (e) { toast(e.message, true); }
}

async function markEvent(contactId, eventId) {
  try {
    await api(`/life-events/${eventId}`, { method: "PATCH", body: JSON.stringify({ status: "acted" }) });
    openContactDetail(contactId, "events");
  } catch (e) { toast(e.message, true); }
}

async function extractFacts(contactId) {
  const btn = $("#extract-facts");
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Extracting…`; }
  try {
    const saved = await api(`/contacts/${contactId}/facts/extract`, { method: "POST" });
    toast(saved.length ? `Added ${saved.length} fact${saved.length === 1 ? "" : "s"}` : "No new facts found");
    openContactDetail(contactId, "facts");
  } catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = "✦ Extract facts (AI)"; } }
}

async function invalidateFact(contactId, factId) {
  try {
    await api(`/facts/${factId}/invalidate`, { method: "POST" });
    openContactDetail(contactId, "facts");
  } catch (e) { toast(e.message, true); }
}

function openFactForm(contactId) {
  openModal(`
    <h3>Add fact</h3>
    <form id="fact-form">
      <div class="form-grid">
        <label>Type<select name="fact_type">${FACT_TYPES.map((t) => `<option value="${t}">${titleCase(t)}</option>`).join("")}</select></label>
        <label class="form-full">Fact<input name="value" required placeholder="e.g. Works at Acme as CTO" /></label>
      </div>
      <div class="modal-actions"><button type="button" class="btn" id="m-cancel">Cancel</button>
        <button type="submit" class="btn primary">Add</button></div>
    </form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#fact-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = { fact_type: fd.get("fact_type"), value: fd.get("value"), source: "manual" };
    try {
      await api(`/contacts/${contactId}/facts`, { method: "POST", body: JSON.stringify(body) });
      closeModal(); toast("Fact added"); openContactDetail(contactId, "facts");
    } catch (err) { toast(err.message, true); }
  });
}

/* ── Forms ─────────────────────────────────────────────────────────────── */
function openContactForm(c = null) {
  const v = (k) => (c && c[k] != null ? esc(c[k]) : "");
  openModal(`
    <h3>${c ? "Edit contact" : "New contact"}</h3>
    <form id="contact-form">
      <div class="form-grid">
        <label>First name *<input name="first_name" required value="${v("first_name")}" /></label>
        <label>Last name<input name="last_name" value="${v("last_name")}" /></label>
        <label>Relationship
          <select name="relationship_type">${RELATIONSHIPS.map((r) => `<option value="${r}" ${c?.relationship_type === r ? "selected" : ""}>${titleCase(r)}</option>`).join("")}</select></label>
        <label>Contact frequency
          <select name="contact_frequency">${FREQUENCIES.map((f) => `<option value="${f}" ${c?.contact_frequency === f ? "selected" : "" }>${titleCase(f)}</option>`).join("")}</select></label>
        <label>Company<input name="company" value="${v("company")}" /></label>
        <label>Position<input name="position" value="${v("position")}" /></label>
        <label>Location<input name="location" value="${v("location")}" /></label>
        <label>Birthday<input type="date" name="birth_date" value="${v("birth_date")}" /></label>
        <label>Email<input type="email" name="email" value="${v("email")}" /></label>
        <label>Phone<input name="phone" value="${v("phone")}" /></label>
        <label>Telegram<input name="telegram" value="${v("telegram")}" /></label>
        <label>WhatsApp<input name="whatsapp" value="${v("whatsapp")}" /></label>
        <label>LinkedIn URL<input name="linkedin_url" value="${v("linkedin_url")}" /></label>
        <label>Instagram URL<input name="instagram_url" value="${v("instagram_url")}" /></label>
        <label>Facebook URL<input name="facebook_url" value="${v("facebook_url")}" /></label>
        <label>Twitter/X URL<input name="twitter_url" value="${v("twitter_url")}" /></label>
        <label>Тон спілкування<input name="tone" value="${v("tone")}" placeholder="дружній / діловий" /></label>
        <label>Важливість
          <select name="importance">
            <option value="0" ${!c || !c.importance ? "selected" : ""}>Авто (за історією)</option>
            <option value="3" ${c?.importance === 3 ? "selected" : ""}>⭐ Висока</option>
            <option value="2" ${c?.importance === 2 ? "selected" : ""}>Звичайна</option>
            <option value="1" ${c?.importance === 1 ? "selected" : ""}>Низька</option>
          </select></label>
        <label>Стоп-лист
          <select name="do_not_contact">
            <option value="false" ${c?.do_not_contact ? "" : "selected"}>Ні — пропонувати</option>
            <option value="true" ${c?.do_not_contact ? "selected" : ""}>Так — не пропонувати</option>
          </select></label>
        <label class="form-full">Tags (comma separated)<input name="tags" value="${c ? c.tags.map(esc).join(", ") : ""}" /></label>
        <label class="form-full">Notes<textarea name="notes" rows="3">${v("notes")}</textarea></label>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" id="m-cancel">Cancel</button>
        <button type="submit" class="btn primary">${c ? "Save" : "Create"}</button>
      </div>
    </form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#contact-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {};
    for (const [k, val] of fd.entries()) {
      if (k === "tags") body.tags = val.split(",").map((s) => s.trim()).filter(Boolean);
      else if (k === "do_not_contact") body.do_not_contact = val === "true";
      else if (k === "importance") body.importance = parseInt(val, 10);
      else if (val === "") body[k] = null;
      else body[k] = val;
    }
    try {
      if (c) await api(`/contacts/${c.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api("/contacts", { method: "POST", body: JSON.stringify(body) });
      closeModal(); toast(c ? "Saved" : "Contact created");
      if (c) openContactDetail(c.id); else setView("contacts");
    } catch (err) { toast(err.message, true); }
  });
}

function openInteractionForm(contactId) {
  openModal(`
    <h3>Log interaction</h3>
    <form id="itx-form">
      <div class="form-grid">
        <label>Channel<select name="channel">${CHANNELS.map((c) => `<option value="${c}">${titleCase(c)}</option>`).join("")}</select></label>
        <label>Direction<select name="direction"><option value="outbound">Outbound</option><option value="inbound">Inbound</option></select></label>
        <label>When<input type="date" name="occurred_at" value="${new Date().toISOString().slice(0,10)}" /></label>
        <label>Sentiment
          <select name="sentiment">
            <option value="1">Great (+1)</option><option value="0.5">Good (+0.5)</option>
            <option value="0" selected>Neutral (0)</option>
            <option value="-0.5">Tense (-0.5)</option><option value="-1">Bad (-1)</option>
          </select></label>
        <label class="form-full">Summary<textarea name="summary" rows="2" placeholder="What did you talk about?"></textarea></label>
      </div>
      <div class="modal-actions"><button type="button" class="btn" id="m-cancel">Cancel</button>
        <button type="submit" class="btn primary">Log</button></div>
    </form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#itx-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {
      channel: fd.get("channel"),
      direction: fd.get("direction"),
      sentiment: parseFloat(fd.get("sentiment")),
      summary: fd.get("summary") || null,
      occurred_at: fd.get("occurred_at") ? new Date(fd.get("occurred_at")).toISOString() : null,
    };
    try {
      await api(`/contacts/${contactId}/interactions`, { method: "POST", body: JSON.stringify(body) });
      closeModal(); toast("Interaction logged"); openContactDetail(contactId, "interactions");
    } catch (err) { toast(err.message, true); }
  });
}

function openImportForm(contactId) {
  openModal(`
    <h3>Import from a social link</h3>
    <p class="muted">Paste a public profile or post URL (Instagram, LinkedIn, Facebook, X, a website…). We capture a snapshot${state.aiEnabled ? " and let the AI detect life events." : "."}</p>
    <form id="imp-form">
      <input class="form-full" name="url" placeholder="https://…" required style="width:100%" />
      <label class="flex mt" style="color:var(--muted)"><input type="checkbox" name="analyze" ${state.aiEnabled ? "checked" : ""} style="width:auto" /> &nbsp;Detect life events with AI</label>
      <div class="modal-actions"><button type="button" class="btn" id="m-cancel">Cancel</button>
        <button type="submit" class="btn primary">Import</button></div>
    </form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#imp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const btn = e.target.querySelector("button[type=submit]");
    btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = await api(`/contacts/${contactId}/import`, { method: "POST",
        body: JSON.stringify({ url: fd.get("url"), analyze: !!fd.get("analyze") }) });
      closeModal();
      toast(r.detected_events.length ? `Imported · ${r.detected_events.length} event(s) detected` : "Snapshot imported");
      openContactDetail(contactId, "social");
    } catch (err) { toast(err.message, true); btn.textContent = "Import"; }
  });
}

function confirmDelete(c) {
  openModal(`<h3>Delete ${esc(c.full_name)}?</h3>
    <p class="muted">This removes the contact and all their history. This cannot be undone.</p>
    <div class="modal-actions"><button class="btn" id="m-cancel">Cancel</button>
      <button class="btn danger" id="m-del">Delete</button></div>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#m-del").addEventListener("click", async () => {
    try { await api(`/contacts/${c.id}`, { method: "DELETE" }); closeModal(); toast("Contact deleted"); setView("contacts"); }
    catch (e) { toast(e.message, true); }
  });
}


/* ── Пошта ─────────────────────────────────────────────────────────────── */
const MAIL_URGENCY = { high: "терміново", normal: "звичайне", low: "може почекати" };

async function renderInbox() {
  const main = $("#main");
  main.innerHTML = `<div class="view-head"><h2>Пошта</h2>
    <button class="btn" id="inbox-sync">Синхронізувати</button></div>
    <div id="inbox-body" class="empty"><span class="spinner"></span> Завантаження…</div>`;
  $("#inbox-sync").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Читаю пошту…";
    try { const r = await api("/inbox/sync", { method: "POST" });
      toast(`Тредів: ${r.fetched}, нових: ${r.added}, чернеток: ${r.drafted}`); renderInbox(); }
    catch (err) { toast(err.message, true); renderInbox(); }
  });

  let threads, stats;
  try {
    [threads, stats] = await Promise.all([api("/inbox"), api("/inbox/stats")]);
  } catch (e) { return showError(e); }

  if (!threads.length) {
    $("#inbox-body").innerHTML = `<div class="empty">Пошта розібрана — нічого не чекає на відповідь.</div>`;
    return;
  }

  const cards = threads.map((t) => `
    <div class="card mb" data-thread="${t.id}">
      <div class="flex between">
        <div>
          <h3 class="section-title" style="margin:0">${esc(t.subject)}</h3>
          <div class="muted">${esc(t.from_name || t.from_email)}
            · ${t.age_days === 0 ? "сьогодні" : t.age_days + " дн"}
            · ${esc(MAIL_URGENCY[t.urgency] || t.urgency)}
            ${t.contact_name ? " · " + esc(t.contact_name) : ""}
            ${t.in_gmail ? " · у чернетках Gmail" : ""}</div>
        </div>
        <div class="flex" style="gap:6px">
          ${t.draft_text ? `<button class="btn small primary" data-send="${t.id}">Надіслати</button>` : ""}
          <button class="btn small" data-draft="${t.id}">${t.draft_text ? "Переписати" : "Чернетка"}</button>
          <button class="btn small" data-skip="${t.id}">Пропустити</button>
        </div>
      </div>
      ${t.topic ? `<p class="muted">${esc(t.topic)}</p>` : ""}
      ${t.draft_text
        ? `<div class="card" style="background:var(--panel-2)"><pre class="draft">${esc(t.draft_text)}</pre></div>`
        : `<p class="muted">${esc(t.snippet || "")}</p>`}
    </div>`).join("");

  $("#inbox-body").className = "";
  $("#inbox-body").innerHTML = `
    <div class="grid stats-grid">
      <div class="card stat"><div class="label">Чекають</div><div class="value">${stats.waiting}</div></div>
      <div class="card stat"><div class="label">Термінові</div><div class="value">${stats.urgent}</div></div>
      <div class="card stat"><div class="label">Понад 2 дні</div><div class="value">${stats.stale}</div></div>
      <div class="card stat"><div class="label">З чернеткою</div><div class="value">${stats.drafted}</div></div>
    </div>${cards}`;

  main.querySelectorAll("[data-draft]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Пишу…";
    try { await api(`/inbox/${b.dataset.draft}/draft`, { method: "POST" }); renderInbox(); }
    catch (e) { toast(e.message, true); renderInbox(); }
  }));
  main.querySelectorAll("[data-skip]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/inbox/${b.dataset.skip}`, { method: "PATCH", body: JSON.stringify({ status: "ignored" }) });
      renderInbox(); } catch (e) { toast(e.message, true); }
  }));
  main.querySelectorAll("[data-send]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Надіслати цю відповідь?")) return;
    b.disabled = true;
    try { await api(`/inbox/${b.dataset.send}/send`, { method: "POST" }); toast("Надіслано"); renderInbox(); }
    catch (e) { toast(e.message, true); renderInbox(); }
  }));
}

/* ── Goals (трекер цілей) ─────────────────────────────────── */
const GOAL_STATUSES = ["not started", "in progress", "almost", "done", "cancelled",
  "postponed to next year", "postponed to far future"];
const GOAL_HORIZONS = { present: "Present", future: "Future", far_future: "Far Future" };
const GOAL_AREAS = { pers: "Pers", work: "Work" };
const GOAL_PRIORITIES = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10];
const STATUS_CLASS = {
  "done": "s-done", "almost": "s-almost", "in progress": "s-progress",
  "not started": "s-none", "cancelled": "s-cancelled",
  "postponed to next year": "s-postponed", "postponed to far future": "s-postponed",
};

/* ── Tasks ─────────────────────────────────────────────────────────────── */
const TASK_STATUS = {
  immediate: "🔥 зараз", urgent: "⏰ дедлайн", current: "▶ в роботі",
  todo: "○ у черзі", hold: "⏸ чекає", done: "✓ готово",
};

function taskMeta(t) {
  const bits = [TASK_STATUS[t.status] || t.status];
  if (t.project) bits.push("#" + t.project);
  if (t.duration_min) bits.push(t.duration_min >= 60
    ? (t.duration_min / 60).toFixed(1).replace(".0", "") + " год" : t.duration_min + " хв");
  if (t.due_date) bits.push("до " + t.due_date);
  if (t.tags) bits.push(t.tags.split(",").map((x) => "@" + x).join(" "));
  return bits.join(" · ");
}

async function renderTasks() {
  const main = $("#main");
  main.innerHTML = `<div class="view-head"><h2>Tasks</h2>
    <button class="btn primary" id="new-task">+ Нова задача</button></div>
    <div class="empty"><span class="spinner"></span> Loading…</div>`;
  $("#new-task").addEventListener("click", () => openTaskForm());
  let tasks;
  try { tasks = await api("/tasks"); } catch (e) { return showError(e); }

  // Список уже впорядкований сервером: статус → дата → тривалість.
  const now = tasks[0];
  const hero = now ? `<div class="card mb" style="border-left:3px solid var(--accent, #5eead4)">
      <div class="muted" style="font-size:11px;letter-spacing:.08em">ЗАРАЗ</div>
      <h3 class="section-title" style="margin:4px 0">${esc(now.title)}</h3>
      <div class="muted">${esc(taskMeta(now))}</div>
      <div class="flex mt" style="gap:6px">
        <button class="btn small" data-done="${now.id}">✓ Готово</button>
        <button class="btn small" data-cur="${now.id}">▶ Взяв у роботу</button>
      </div>
    </div>` : "";

  const rest = tasks.slice(1).map((t) => `
    <div class="card mb">
      <div class="flex between">
        <div><strong>${esc(t.title)}</strong>
          <div class="muted">${esc(taskMeta(t))}</div></div>
        <div class="flex" style="gap:6px">
          <button class="btn small" data-cur="${t.id}">▶</button>
          <button class="btn small" data-done="${t.id}">✓</button>
          <button class="btn small" data-edit="${t.id}">Edit</button>
          <button class="btn small danger" data-del="${t.id}">✕</button>
        </div>
      </div>
    </div>`).join("");

  main.querySelector(".empty").outerHTML = tasks.length
    ? hero + rest
    : `<div class="empty">Задач немає. Додай першу — або вони приїдуть із focus.md на ПК.</div>`;

  const patch = async (id, body) => {
    try { await api(`/tasks/${id}`, { method: "PATCH", body: JSON.stringify(body) }); renderTasks(); }
    catch (e) { toast(e.message, true); }
  };
  main.querySelectorAll("[data-done]").forEach((b) =>
    b.addEventListener("click", () => patch(b.dataset.done, { status: "done" })));
  main.querySelectorAll("[data-cur]").forEach((b) =>
    b.addEventListener("click", () => patch(b.dataset.cur, { status: "current" })));
  main.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openTaskForm(tasks.find((t) => t.id === Number(b.dataset.edit)))));
  main.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Видалити задачу?")) return;
    try { await api(`/tasks/${b.dataset.del}`, { method: "DELETE" }); renderTasks(); }
    catch (e) { toast(e.message, true); }
  }));
}

function openTaskForm(t = null) {
  openModal(`
    <h3>${t ? "Редагувати задачу" : "Нова задача"}</h3>
    <form id="task-form">
      <label>Назва<input name="title" required value="${esc(t?.title || "")}" /></label>
      <label>Статус<select name="status">${Object.entries(TASK_STATUS).map(([k, v]) =>
        `<option value="${k}"${t?.status === k ? " selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Проєкт<input name="project" value="${esc(t?.project || "")}" /></label>
      <label>Скільки займе, хв<input name="duration_min" type="number" min="1"
        value="${t?.duration_min || ""}" /></label>
      <label>Дедлайн<input name="due_date" type="date" value="${t?.due_date || ""}" /></label>
      <label>Теги (через кому)<input name="tags" value="${esc(t?.tags || "")}" /></label>
      <div class="flex mt" style="gap:8px">
        <button class="btn primary" type="submit">Зберегти</button>
        <button class="btn" type="button" id="task-cancel">Скасувати</button>
      </div>
    </form>`);
  $("#task-cancel").addEventListener("click", closeModal);
  $("#task-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {};
    for (const [k, v] of f.entries()) body[k] = v === "" ? null : v;
    if (body.duration_min) body.duration_min = Number(body.duration_min);
    try {
      if (t) await api(`/tasks/${t.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api("/tasks", { method: "POST", body: JSON.stringify(body) });
      closeModal(); renderTasks();
    } catch (err) { toast(err.message, true); }
  });
}

const goalOpt = (value, label, current) =>
  `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;

function lastNote(notes) {
  const lines = String(notes || "").split("\n").map((l) => l.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1] : "";
}

function goalStatCard(label, b) {
  const rows = GOAL_STATUSES.filter((st) => b.counts[st])
    .map((st) => `<tr><td>${esc(st)}</td><td class="num">${b.counts[st]}</td>
      <td class="num muted">${b.percents[st]}%</td></tr>`).join("");
  return `<div class="card">
    <div class="flex between"><h3 class="section-title" style="margin:0">${esc(label)}</h3>
      <span class="big">${b.closed_pct}%</span></div>
    <div class="muted mb">Цілей: <b>${b.total}</b> · done: <b>${b.done}</b> (${b.done_pct}%)</div>
    <table class="mini">${rows || '<tr><td class="muted">порожньо</td></tr>'}
      <tr class="sum"><td>done and cancelled</td><td class="num">${b.closed}</td>
        <td class="num muted">${b.closed_pct}%</td></tr></table>
  </div>`;
}

async function renderGoals() {
  const main = $("#main");
  main.innerHTML = `<div class="view-head"><h2>Цілі</h2>
    <div class="flex" style="gap:8px">
      <button class="btn" id="coach-settings">Коуч</button>
      <button class="btn" id="coach-ask">Спитати зараз</button>
      <button class="btn primary" id="new-goal">+ ціль</button>
    </div></div>
    <div id="goals-body" class="empty"><span class="spinner"></span> Завантаження…</div>`;
  $("#new-goal").addEventListener("click", () => openGoalForm());
  $("#coach-settings").addEventListener("click", openCoachSettings);
  $("#coach-ask").addEventListener("click", async () => {
    try {
      const r = await api("/coach/ask", { method: "POST" });
      toast(r.asked ? r.question : (r.reason || "Питати нема про що"), !r.asked);
    } catch (e) { toast(e.message, true); }
  });

  let goals, board;
  try {
    [goals, board] = await Promise.all([api("/goals"), api("/coach/board")]);
  } catch (e) { return showError(e); }
  goals = [...goals].sort((a, b) => b.priority - a.priority || a.id - b.id);

  const rows = goals.map((g) => `<tr data-goal="${g.id}">
    <td><select class="cell" data-f="horizon">${goalOpt("", "—", g.horizon || "")}
      ${Object.entries(GOAL_HORIZONS).map(([v, l]) => goalOpt(v, l, g.horizon || "")).join("")}</select></td>
    <td><select class="cell" data-f="priority">
      ${GOAL_PRIORITIES.map((n) => goalOpt(String(n), n, String(g.priority))).join("")}</select></td>
    <td><select class="cell" data-f="area">${goalOpt("", "—", g.area || "")}
      ${Object.entries(GOAL_AREAS).map(([v, l]) => goalOpt(v, l, g.area || "")).join("")}</select></td>
    <td class="goal-title"><button class="linklike" data-edit="${g.id}">${esc(g.title)}</button></td>
    <td><input class="cell" data-f="owner" value="${esc(g.owner || "")}" placeholder="—" /></td>
    <td><input class="cell" data-f="owner2" value="${esc(g.owner2 || "")}" placeholder="—" /></td>
    <td><select class="cell status ${STATUS_CLASS[g.status] || ""}" data-f="status">
      ${GOAL_STATUSES.map((st) => goalOpt(st, st, g.status)).join("")}</select></td>
    <td class="note"><button class="linklike" data-notes="${g.id}"
      title="Відкрити журнал">${esc(lastNote(g.coach_notes)) || "—"}</button></td>
    <td><button class="btn small danger" data-del="${g.id}">✕</button></td>
  </tr>`).join("");

  $("#goals-body").className = "";
  $("#goals-body").innerHTML = `
    <label class="theme-line">Тема року
      <input id="coach-theme" value="${esc(board.theme)}"
        placeholder="напр. Здоровʼя. Комфорт. Гроші." /></label>
    <div class="grid stats-grid">
      ${goalStatCard("Pers", board.pers)}
      ${goalStatCard("Work", board.work)}
      ${goalStatCard("Pers and Work", board.all)}
    </div>
    ${goals.length ? `<div class="card scroll-x"><table class="gtable">
      <thead><tr><th>На що впливає</th><th>Пріоритет</th><th>Тип</th><th>Ціль</th>
        <th>Відповідальний</th><th>Відповідальний #2</th><th>Статус</th>
        <th>Останній запис</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`
      : `<div class="empty">Ще немає цілей. Додай першу — коуч почне питати про неї щодня.</div>`}`;

  $("#coach-theme").addEventListener("change", async (e) => {
    try {
      await api("/coach/settings", { method: "PATCH", body: JSON.stringify({ theme: e.target.value }) });
      toast("Тему збережено");
    } catch (err) { toast(err.message, true); }
  });

  main.querySelectorAll("[data-goal] .cell").forEach((input) =>
    input.addEventListener("change", async () => {
      const id = input.closest("[data-goal]").dataset.goal;
      const field = input.dataset.f;
      let value = input.value;
      if (field === "priority") value = parseInt(value, 10);
      else if (value === "") value = null;
      try {
        await api(`/goals/${id}`, { method: "PATCH", body: JSON.stringify({ [field]: value }) });
        // Статус і тип міняють цифри дашбордів — перемальовуємо все.
        if (field === "status" || field === "area") renderGoals();
        else toast("Збережено");
      } catch (e) { toast(e.message, true); renderGoals(); }
    }));

  main.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Видалити ціль?")) return;
    try { await api(`/goals/${b.dataset.del}`, { method: "DELETE" }); renderGoals(); }
    catch (e) { toast(e.message, true); }
  }));
  main.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () =>
    openGoalForm(goals.find((g) => g.id === Number(b.dataset.edit)))));
  main.querySelectorAll("[data-notes]").forEach((b) => b.addEventListener("click", () =>
    openGoalNotes(goals.find((g) => g.id === Number(b.dataset.notes)))));
}

function openGoalNotes(g) {
  openModal(`<h3>${esc(g.title)}</h3>
    <p class="muted">Журнал коуча — рядок на кожну звірку.</p>
    <textarea id="notes-body" rows="12" style="width:100%">${esc(g.coach_notes || "")}</textarea>
    <div class="modal-actions"><button class="btn" id="m-cancel">Закрити</button>
      <button class="btn primary" id="m-save">Зберегти</button></div>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#m-save").addEventListener("click", async () => {
    try {
      await api(`/goals/${g.id}`, {
        method: "PATCH", body: JSON.stringify({ coach_notes: $("#notes-body").value }),
      });
      closeModal(); renderGoals();
    } catch (e) { toast(e.message, true); }
  });
}

async function openCoachSettings() {
  let cfg;
  try { cfg = await api("/coach/settings"); } catch (e) { return toast(e.message, true); }
  const hourPicker = (name, value) => `<select name="${name}">${
    Array.from({ length: 24 }, (_, h) =>
      goalOpt(String(h), `${String(h).padStart(2, "0")}:00`, String(value))).join("")}</select>`;
  const days = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"];
  openModal(`<h3>Коуч</h3>
    <form id="coach-form"><div class="form-grid">
      <label class="form-full"><input type="checkbox" name="enabled" ${cfg.enabled ? "checked" : ""} />
        Питати щодня і підбивати тиждень</label>
      <label>Питання дня ${hourPicker("ask_hour", cfg.ask_hour)}</label>
      <label>Нагадування ${hourPicker("nudge_hour", cfg.nudge_hour)}</label>
      <label>Підсумок тижня<select name="weekly_weekday">${
        days.map((d, i) => goalOpt(String(i), d, String(cfg.weekly_weekday))).join("")}</select></label>
      <label>о котрій ${hourPicker("weekly_hour", cfg.weekly_hour)}</label>
    </div>
    <div class="modal-actions"><button type="button" class="btn" id="m-cancel">Скасувати</button>
      <button type="submit" class="btn primary">Зберегти</button></div></form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#coach-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {
      enabled: fd.get("enabled") === "on",
      ask_hour: parseInt(fd.get("ask_hour"), 10),
      nudge_hour: parseInt(fd.get("nudge_hour"), 10),
      weekly_weekday: parseInt(fd.get("weekly_weekday"), 10),
      weekly_hour: parseInt(fd.get("weekly_hour"), 10),
    };
    try {
      await api("/coach/settings", { method: "PATCH", body: JSON.stringify(body) });
      closeModal(); toast("Збережено");
    } catch (err) { toast(err.message, true); }
  });
}

function openGoalForm(g = null) {
  openModal(`
    <h3>${g ? "Ціль" : "Нова ціль"}</h3>
    <form id="goal-form">
      <div class="form-grid">
        <label class="form-full">Ціль *<input name="title" required value="${g ? esc(g.title) : ""}" /></label>
        <label>Статус<select name="status">${GOAL_STATUSES.map((st) =>
          goalOpt(st, st, g?.status || "not started")).join("")}</select></label>
        <label>Пріоритет<select name="priority">${GOAL_PRIORITIES.map((n) =>
          goalOpt(String(n), n, String(g?.priority ?? 50))).join("")}</select></label>
        <label>На що впливає<select name="horizon">${goalOpt("", "—", g?.horizon || "")}
          ${Object.entries(GOAL_HORIZONS).map(([v, l]) =>
            goalOpt(v, l, g?.horizon || "")).join("")}</select></label>
        <label>Тип<select name="area">${goalOpt("", "—", g?.area || "")}
          ${Object.entries(GOAL_AREAS).map(([v, l]) =>
            goalOpt(v, l, g?.area || "")).join("")}</select></label>
        <label>Відповідальний<input name="owner" value="${esc(g?.owner || "")}" /></label>
        <label>Відповідальний #2<input name="owner2" value="${esc(g?.owner2 || "")}" /></label>
        <label>Дедлайн<input type="date" name="target_date" value="${g?.target_date || ""}" /></label>
        <label class="form-full">Опис<textarea name="description" rows="2">${g ? esc(g.description || "") : ""}</textarea></label>
      </div>
      <div class="modal-actions"><button type="button" class="btn" id="m-cancel">Скасувати</button>
        <button type="submit" class="btn primary">${g ? "Зберегти" : "Створити"}</button></div>
    </form>`);
  $("#m-cancel").addEventListener("click", closeModal);
  $("#goal-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {
      title: fd.get("title"), status: fd.get("status"),
      priority: parseInt(fd.get("priority"), 10),
      horizon: fd.get("horizon") || null,
      area: fd.get("area") || null,
      owner: fd.get("owner") || null,
      owner2: fd.get("owner2") || null,
      target_date: fd.get("target_date") || null,
      description: fd.get("description") || null,
    };
    try {
      if (g) await api(`/goals/${g.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api("/goals", { method: "POST", body: JSON.stringify(body) });
      closeModal(); renderGoals();
    } catch (err) { toast(err.message, true); }
  });
}

async function linkGoalToContact(contactId) {
  let goals;
  try { goals = await api("/goals?active=true"); } catch (e) { return toast(e.message, true); }
  if (!goals.length) return toast("Спершу створи ціль у розділі Goals", true);
  openModal(`<h3>Прив’язати до цілі</h3>
    <div class="card">${goals.map((g) =>
      `<button class="btn block mb" data-g="${g.id}">${esc(g.title)}</button>`).join("")}</div>
    <div class="modal-actions"><button class="btn" id="m-cancel">Cancel</button></div>`);
  $("#m-cancel").addEventListener("click", closeModal);
  document.querySelectorAll("[data-g]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/goals/${b.dataset.g}/contacts/${contactId}`, { method: "POST" });
      closeModal(); toast("Прив’язано"); openContactDetail(contactId); }
    catch (e) { toast(e.message, true); }
  }));
}

/* ── Integrations ──────────────────────────────────────────────────────── */
async function renderIntegrations() {
  const main = $("#main");
  main.innerHTML = `<div class="view-head"><h2>Integrations</h2></div><div class="empty"><span class="spinner"></span></div>`;
  let g, ch, tg, mcp, jobs;
  try {
    [g, ch, tg, mcp, jobs] = await Promise.all([
      api("/integrations/google/status"),
      api("/integrations/chater/status"),
      api("/integrations/telegram/status"),
      api("/integrations/mcp/status"),
      api("/aijobs/status"),
    ]);
  } catch (e) { return showError(e); }

  main.innerHTML = `<div class="view-head"><h2>Integrations</h2></div>
    <div class="grid" style="max-width:680px;gap:16px">
      <div class="card"><h3 class="section-title">Google (Gmail + Calendar)</h3><div id="g-body"></div></div>
      <div class="card"><h3 class="section-title">Chater (Telegram bot database)</h3><div id="ch-body"></div></div>
      <div class="card"><h3 class="section-title">Telegram digest & bot</h3><div id="tg-body"></div></div>
      <div class="card"><h3 class="section-title">Claude (MCP)</h3><div id="mcp-body"></div></div>
      <div class="card"><h3 class="section-title">Фоновий AI</h3><div id="aijobs-body"></div></div>
    </div>`;

  renderGooglePanel(g);
  renderChaterPanel(ch);
  renderTelegramPanel(tg);
  renderMcpPanel(mcp);
  renderAiJobsPanel(jobs);
}

function renderAiJobsPanel(st) {
  const body = $("#aijobs-body");
  const modeLabel = { api: "API (модель через ключ)", claude_code: "Claude Code (підписка, через MCP)", off: "вимкнено" }[st.mode] || st.mode;
  const c = st.counts || {};
  const use = (u) => `${u.runs} запусків · ${u.tokens_in.toLocaleString("uk-UA")} in / ${u.tokens_out.toLocaleString("uk-UA")} out · $${u.cost_usd}`;
  const explain = `
    <details class="mt"><summary class="muted">Що це</summary>
      <p class="muted">Фонові задачі (чернетки на вхідні, розбір пошти, досьє, дописи, коуч) виконує або модель через API-ключ,
      або Claude Code на підписці — через наш MCP. Перемикач <code>BACKGROUND_AI</code> у <code>.env</code>: <code>api</code> / <code>claude_code</code> / <code>off</code>.
      Коли Claude Code не впорався за ${'3'} спроби, задача відкочується на API (<code>BACKGROUND_AI_FALLBACK=api</code>).</p>
      <p class="muted">Гальма для квоти: пауза тут, тихі години (<code>AIJOBS_QUIET_HOURS=23-7</code>), стеля ${st.max_per_hour} запусків на годину, модель <code>CLAUDE_CODE_MODEL</code>.</p>
    </details>`;
  if (st.mode !== "claude_code") {
    body.innerHTML = `<p class="muted">Режим: <b>${esc(modeLabel)}</b>. Черга не використовується — все рахується одразу.</p>${explain}`;
    return;
  }
  body.innerHTML = `
    <p class="muted">Режим: <b>${esc(modeLabel)}</b>${st.paused ? " · <b>на паузі</b>" : ""}${st.quiet_now ? " · тихі години" : ""}</p>
    <p class="muted">Черга: ${c.queued || 0} чекають · ${c.running || 0} виконується · ${c.done || 0} готово · ${c.failed || 0} впало</p>
    <p class="muted">Сьогодні: ${use(st.today)}<br>Тиждень: ${use(st.week)}</p>
    <div class="flex mt" style="gap:6px">
      <button class="btn small" id="aijobs-toggle">${st.paused ? "Продовжити" : "Пауза"}</button>
      <button class="btn small" id="aijobs-log">Журнал</button>
    </div>
    <div id="aijobs-log-body" class="mt"></div>${explain}`;
  $("#aijobs-toggle").addEventListener("click", async () => {
    try { renderAiJobsPanel(await api(st.paused ? "/aijobs/resume" : "/aijobs/pause", { method: "POST" })); }
    catch (e) { toast(e.message, true); }
  });
  $("#aijobs-log").addEventListener("click", async () => {
    try {
      const rows = await api("/aijobs");
      const box = $("#aijobs-log-body");
      if (!rows.length) { box.innerHTML = `<p class="muted">Журнал порожній.</p>`; return; }
      box.innerHTML = `<table class="table"><thead><tr><th>#</th><th>Що</th><th>Стан</th><th>Хто</th><th>Результат</th></tr></thead><tbody>${
        rows.map((j) => `<tr><td>${j.id}</td><td>${esc(j.kind)}</td><td>${esc(j.status)}${j.attempts > 1 ? ` (${j.attempts})` : ""}</td><td>${esc(j.backend || "—")}</td><td class="muted">${esc((j.error || j.result || "").slice(0, 160))}</td></tr>`).join("")
      }</tbody></table>`;
    } catch (e) { toast(e.message, true); }
  });
}

function renderMcpPanel(st) {
  const body = $("#mcp-body");
  const seen = st.last_seen ? new Date(st.last_seen).toLocaleString("uk-UA") : "ще не звертався";
  const howTo = `
    <details class="mt"><summary class="muted">Як підключити</summary>
      <ol class="muted" style="padding-left:18px">
        <li>claude.ai → Settings → Connectors → <b>Add custom connector</b> → встав адресу вище. Поля під токен там немає — він уже в адресі.</li>
        <li>Claude Code: <code>claude mcp add --transport http networking &lt;адреса&gt;</code></li>
        <li>Далі в чаті: «кому мені пора написати?», «напиши Марії про зустріч» — Claude сам прочитає памʼять і збереже чернетку.</li>
      </ol>
      <p class="muted">Адреса = пароль. Не публікуй її. Витекла — натисни «Перевипустити», стара помре одразу.</p>
    </details>`;
  if (!st.configured) {
    body.innerHTML = `<p class="muted">${st.tools} інструментів для Claude: памʼять про людей, чернетки, пошта, задачі, цілі.
      Генерація — на боці підписки Claude, наші AI-кредити не витрачаються.</p>
      <button class="btn primary" id="mcp-issue">Створити адресу</button>${howTo}`;
  } else {
    body.innerHTML = `
      <p class="muted">${st.tools} інструментів · Claude звертався: ${esc(seen)}</p>
      <div class="flex" style="gap:6px;align-items:center">
        <input id="mcp-url" readonly value="${esc(st.url)}" style="flex:1;font-family:monospace;font-size:12px" />
        <button class="btn small" id="mcp-copy">Копіювати</button>
      </div>
      <div class="flex mt" style="gap:6px">
        <button class="btn small" id="mcp-issue">Перевипустити</button>
        <button class="btn small danger" id="mcp-revoke">Відключити</button>
      </div>${howTo}`;
    $("#mcp-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(st.url); toast("Скопійовано"); }
      catch (_) { $("#mcp-url").select(); toast("Виділено — скопіюй вручну"); }
    });
    $("#mcp-revoke").addEventListener("click", async () => {
      if (!confirm("Відключити Claude? Поточна адреса перестане працювати.")) return;
      try { renderMcpPanel(await api("/integrations/mcp/revoke", { method: "POST" })); }
      catch (e) { toast(e.message, true); }
    });
  }
  $("#mcp-issue").addEventListener("click", async () => {
    if (st.configured && !confirm("Перевипустити адресу? Стара перестане працювати одразу.")) return;
    try { renderMcpPanel(await api("/integrations/mcp/issue", { method: "POST" })); toast("Адресу створено"); }
    catch (e) { toast(e.message, true); }
  });
}

function renderGooglePanel(st) {
  const body = $("#g-body");
  if (!st.configured) {
    body.innerHTML = `<p class="muted">Not configured. Set <code>GOOGLE_CLIENT_ID</code> /
      <code>GOOGLE_CLIENT_SECRET</code> (see <code>docs/SETUP_GOOGLE.md</code>), then reload.</p>`;
    return;
  }
  if (!st.connected) {
    body.innerHTML = `<p class="muted">Connect Google so emails and meetings with your
      contacts become interactions automatically.</p>
      <a class="btn primary" href="/api/integrations/google/authorize">Connect Google</a>`;
    return;
  }
  body.innerHTML = `
    <div class="flex between mb">
      <div><strong>Connected</strong> <span class="badge on">${esc(st.account_email || "")}</span></div>
      <button class="btn danger small" id="g-disconnect">Disconnect</button>
    </div>
    <p class="muted">Last sync: ${st.last_sync_at ? new Date(st.last_sync_at).toLocaleString() : "never"}</p>
    <div class="flex"><button class="btn primary" id="g-sync">↻ Sync now</button>
      <button class="btn" id="g-daily">Run daily job</button></div>
    <div id="g-result" class="mt"></div>`;
  $("#g-disconnect").addEventListener("click", async () => {
    await api("/integrations/google/disconnect", { method: "POST" }); toast("Disconnected"); renderIntegrations();
  });
  $("#g-sync").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span> Syncing…`;
    try {
      const r = await api("/integrations/google/sync", { method: "POST" });
      $("#g-result").innerHTML = `<div class="ai-box">Processed ${r.contacts_processed} contacts ·
        ${r.emails_added} emails · ${r.meetings_added} meetings.${r.errors.length ? "<br>⚠ " + r.errors.map(esc).join("<br>⚠ ") : ""}</div>`;
      toast("Sync complete");
    } catch (err) { toast(err.message, true); } btn.textContent = "↻ Sync now";
  });
  $("#g-daily").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = await api("/maintenance/run-daily?send_digest=true", { method: "POST" });
      $("#g-result").innerHTML = `<div class="ai-box">${esc(JSON.stringify(r, null, 2))}</div>`; toast("Daily job ran");
    } catch (err) { toast(err.message, true); } btn.textContent = "Run daily job";
  });
}

function renderChaterPanel(st) {
  const body = $("#ch-body");
  if (!st.configured) {
    body.innerHTML = `<p class="muted">Not configured. Set <code>CHATER_DATABASE_URL</code> to your
      Chater Postgres connection to import existing contacts and Telegram history.</p>`;
    return;
  }
  body.innerHTML = `
    <p class="muted">Import contacts and Telegram message history from your Chater bot.</p>
    <div class="flex"><button class="btn" id="ch-inspect">Inspect (dry run)</button>
      <button class="btn primary" id="ch-import">Import now</button>
      <button class="btn" id="ch-dedupe">Remove duplicates</button></div>
    <div id="ch-result" class="mt"></div>`;
  $("#ch-inspect").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = await api("/integrations/chater/inspect");
      $("#ch-result").innerHTML = `<div class="ai-box">${esc(JSON.stringify(r, null, 2))}</div>`;
    } catch (err) { toast(err.message, true); } btn.textContent = "Inspect (dry run)";
  });
  $("#ch-import").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span> Importing…`;
    try {
      const r = await api("/integrations/chater/import", { method: "POST" });
      $("#ch-result").innerHTML = `<div class="ai-box">Created ${r.contacts_created} · updated ${r.contacts_updated} ·
        ${r.interactions_added} interactions · removed ${r.duplicates_removed} duplicates.${r.errors.length ? "<br>⚠ " + r.errors.map(esc).join("<br>⚠ ") : ""}</div>`;
      toast("Chater import complete");
    } catch (err) { toast(err.message, true); } btn.textContent = "Import now";
  });
  $("#ch-dedupe").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = await api("/integrations/chater/dedupe", { method: "POST" });
      $("#ch-result").innerHTML = `<div class="ai-box">Removed ${r.duplicates_removed} duplicate contacts.</div>`;
      toast("Duplicates removed");
    } catch (err) { toast(err.message, true); } btn.textContent = "Remove duplicates";
  });
}

function renderTelegramPanel(st) {
  const body = $("#tg-body");
  if (!st.configured) {
    body.innerHTML = `<p class="muted">Not configured. Set <code>TELEGRAM_BOT_TOKEN</code> and
      <code>TELEGRAM_CHAT_ID</code> to receive the daily digest and use the command bot
      (/today, /due, /find).</p>`;
    return;
  }
  body.innerHTML = `
    <p class="muted">Digest delivery is ${st.chat_id_set ? "enabled" : "missing a chat id"}. Commands: /today, /due, /find.</p>
    <p class="muted">Голосові: ${st.voice_provider
      ? `розшифровує <b>${esc(st.voice_provider)}</b>${st.voice_providers.length > 1 ? " (запас: " + st.voice_providers.slice(1).map(esc).join(", ") + ")" : ""}`
      : "немає провайдера — додай <code>DEEPGRAM_API_KEY</code> у .env"}</p>
    <p class="muted">ALiSiO CRM: ${st.alisio
      ? "підключено — у чатах її лідів бот мовчить, відповідає ALiSiO"
      : "не підключено — додай <code>ALISIO_WEBHOOK_SECRET</code> у .env"}</p>
    <p class="muted">Holos: ${st.holos
      ? "підключено — сторіс у профіль ставить Holos цим ботом, твої власні повідомлення йдуть туди для ідей постів"
      : "не підключено — <code>HOLOS_RELAY_URL</code> і <code>HOLOS_RELAY_SECRET</code> з кабінету Holos у .env"}</p>
    <button class="btn primary" id="tg-test">Send test message</button>
    <div id="tg-result" class="mt"></div>`;
  $("#tg-test").addEventListener("click", async (e) => {
    const btn = e.target; btn.innerHTML = `<span class="spinner"></span>`;
    try { await api("/integrations/telegram/test", { method: "POST" }); toast("Sent — check Telegram"); }
    catch (err) { toast(err.message, true); } btn.textContent = "Send test message";
  });
}

/* ── Misc ──────────────────────────────────────────────────────────────── */
function showError(e) {
  $("#main").innerHTML = `<div class="empty">⚠ ${esc(e.message)}</div>`;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function handleOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const g = params.get("google");
  if (!g) return null;
  if (g === "connected") toast("Google connected: " + (params.get("email") || ""));
  else if (g === "error") toast("Google error: " + (params.get("detail") || ""), true);
  history.replaceState({}, "", location.pathname);
  return g;
}

async function init() {
  try {
    const s = await api("/ai/status");
    state.aiEnabled = s.ai_enabled;
    const badge = $("#ai-badge");
    badge.textContent = s.ai_enabled ? "AI on" : "AI off";
    badge.className = "badge " + (s.ai_enabled ? "on" : "off");
  } catch (_) {}
  const oauth = handleOAuthReturn();
  setView(oauth ? "integrations" : "dashboard");
}
init();
