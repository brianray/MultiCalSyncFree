/* CalSync single-page UI. No framework, no build step. */
(() => {
  const $ = (s) => document.querySelector(s);
  const state = { me: null, calendars: {} };

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const body = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "never");

  // ---------------------------------------------------------------- loading
  async function load() {
    state.me = await api("/api/me");
    const { user, providers } = state.me;
    if (!user) {
      $("#login").classList.remove("hidden");
      $("#dash").classList.add("hidden");
      const missing = Object.entries(providers).filter(([, ok]) => !ok).map(([p]) => p);
      $("#login-note").textContent = missing.length ? `Not configured on this server yet: ${missing.join(", ")}. See the README.` : "";
      $("#whoami").textContent = "";
      return;
    }
    $("#login").classList.add("hidden");
    $("#dash").classList.remove("hidden");
    $("#whoami").innerHTML = `${esc(user.email)} · <a href="#" id="logout">sign out</a>`;
    $("#logout").onclick = async (e) => { e.preventDefault(); await api("/auth/logout", { method: "POST" }); location.reload(); };
    $("#window").textContent = `Mirroring ${state.me.window.past_days} days back, ${state.me.window.future_days} days ahead.`;
    renderAccounts();
    await fillAccountSelects();
    renderLinks();
    loadErrors();
  }

  // ---------------------------------------------------------------- accounts
  function renderAccounts() {
    const ul = $("#accounts");
    if (!state.me.accounts.length) { ul.innerHTML = `<li class="empty">No accounts yet.</li>`; return; }
    ul.innerHTML = state.me.accounts.map((a) => `
      <li>
        <span><span class="badge ${a.provider}">${a.provider}</span> &nbsp; ${esc(a.email)} <span class="muted">${esc(a.name)}</span></span>
        <button class="btn small danger" data-del-acct="${a.id}">Disconnect</button>
      </li>`).join("");
    ul.querySelectorAll("[data-del-acct]").forEach((b) => b.onclick = async () => {
      if (!confirm("Disconnect this account? Any syncs using it will be removed.")) return;
      await api(`/api/accounts/${b.dataset.delAcct}`, { method: "DELETE" });
      load();
    });
  }

  async function calendarsFor(accountId) {
    if (!state.calendars[accountId]) {
      state.calendars[accountId] = api(`/api/accounts/${accountId}/calendars`).then((r) => r.calendars).catch((e) => {
        delete state.calendars[accountId];
        throw e;
      });
    }
    return state.calendars[accountId];
  }

  async function fillAccountSelects() {
    const opts = state.me.accounts.map((a) => `<option value="${a.id}">${esc(a.provider)}: ${esc(a.email)}</option>`).join("");
    $("#src-acct").innerHTML = opts;
    $("#tgt-acct").innerHTML = opts;
    if (state.me.accounts.length > 1) $("#tgt-acct").selectedIndex = 1;
    await Promise.all([fillCalSelect("src"), fillCalSelect("tgt")]);
  }

  async function fillCalSelect(side) {
    const acctId = $(`#${side}-acct`).value;
    const sel = $(`#${side}-cal`);
    sel.innerHTML = `<option>loading…</option>`;
    if (!acctId) { sel.innerHTML = ""; return; }
    try {
      const cals = await calendarsFor(acctId);
      sel.innerHTML = cals.filter((c) => c.writable).map((c) => `<option value="${esc(c.id)}" ${c.primary ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    } catch (e) {
      sel.innerHTML = `<option value="">could not load: ${esc(e.message)}</option>`;
    }
  }
  $("#src-acct").onchange = () => fillCalSelect("src");
  $("#tgt-acct").onchange = () => fillCalSelect("tgt");

  // ---------------------------------------------------------------- links
  function calName(accountId, calendarId) {
    const list = state.calendars[accountId];
    if (list && typeof list.then !== "function") {
      const c = list.find((x) => x.id === calendarId);
      if (c) return c.name;
    }
    return calendarId.length > 24 ? calendarId.slice(0, 24) + "…" : calendarId;
  }
  function acctEmail(id) { const a = state.me.accounts.find((x) => x.id === id); return a ? a.email : "?"; }

  async function renderLinks() {
    const box = $("#links");
    const links = state.me.links;
    if (!links.length) { box.innerHTML = `<div class="empty">No syncs yet. Create one above.</div>`; return; }
    // Make sure calendar names are available for display.
    await Promise.all(links.flatMap((l) => [l.source_account_id, l.target_account_id]).filter((v, i, a) => a.indexOf(v) === i)
      .map((id) => calendarsFor(id).catch(() => [])));
    box.innerHTML = links.map((l) => `
      <div class="link">
        <div>
          <div class="title">${esc(l.label)} <span class="badge ${l.last_status}">${esc(l.last_status)}</span> ${l.enabled ? "" : '<span class="badge">paused</span>'}</div>
          <div class="meta">${esc(acctEmail(l.source_account_id))} / <b>${esc(calName(l.source_account_id, l.source_calendar_id))}</b>
            → ${esc(acctEmail(l.target_account_id))} / <b>${esc(calName(l.target_account_id, l.target_calendar_id))}</b></div>
          <div class="meta">${l.mode === "busy" ? "Busy blocks only" : "Full details"} · ${l.mirrored} events mirrored · last sync ${when(l.last_sync)}${l.has_cursor ? "" : " · full sync pending"}</div>
        </div>
        <div class="ops">
          <button class="btn small" data-sync="${l.id}">Sync now</button>
          <button class="btn small" data-reconcile="${l.id}" title="Rebuild the mapping from the target calendar and remove orphans">Reconcile</button>
          <button class="btn small" data-toggle="${l.id}" data-enabled="${l.enabled}">${l.enabled ? "Pause" : "Resume"}</button>
          <button class="btn small danger" data-del="${l.id}">Delete</button>
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-sync]").forEach((b) => b.onclick = () => run(b, `/api/links/${b.dataset.sync}/sync`));
    box.querySelectorAll("[data-reconcile]").forEach((b) => b.onclick = () => run(b, `/api/links/${b.dataset.reconcile}/reconcile`));
    box.querySelectorAll("[data-toggle]").forEach((b) => b.onclick = async () => {
      await api(`/api/links/${b.dataset.toggle}`, { method: "PATCH", body: JSON.stringify({ enabled: b.dataset.enabled !== "true" }) });
      load();
    });
    box.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      if (!confirm("Delete this sync?")) return;
      const remove = confirm("Also remove the mirrored events it created in the target calendar?\n\nOK = remove them\nCancel = leave them in place");
      await api(`/api/links/${b.dataset.del}?remove_mirrors=${remove ? 1 : 0}`, { method: "DELETE" });
      load();
    });
  }

  async function run(button, url) {
    const label = button.textContent;
    button.disabled = true; button.textContent = "Working…";
    try {
      const r = await api(url, { method: "POST" });
      const x = r.result;
      button.textContent = `${x.status}: +${x.created} ~${x.updated} -${x.deleted}`;
      setTimeout(load, 1500);
    } catch (e) {
      button.textContent = "Failed"; alert(e.message);
    } finally {
      setTimeout(() => { button.disabled = false; button.textContent = label; }, 2500);
    }
  }

  $("#sync-all").onclick = async () => {
    const b = $("#sync-all"); b.disabled = true; b.textContent = "Syncing…";
    try { for (const l of state.me.links) await api(`/api/links/${l.id}/sync`, { method: "POST" }); } catch (e) { alert(e.message); }
    b.disabled = false; b.textContent = "Sync all now"; load();
  };

  $("#link-form").onsubmit = async (e) => {
    e.preventDefault();
    const msg = $("#form-msg"); msg.textContent = "Creating…";
    try {
      const r = await api("/api/links", {
        method: "POST",
        body: JSON.stringify({
          source_account_id: $("#src-acct").value, source_calendar_id: $("#src-cal").value,
          target_account_id: $("#tgt-acct").value, target_calendar_id: $("#tgt-cal").value,
          mode: $("#mode").value, label: $("#label").value, two_way: $("#two-way").checked,
        }),
      });
      msg.textContent = r.links.length ? `Created ${r.links.length} sync(s). First sync is running in the background.` : "That sync already exists.";
      $("#label").value = "";
      setTimeout(load, 2000);
    } catch (err) { msg.textContent = err.message; }
  };

  // ---------------------------------------------------------------- errors
  async function loadErrors() {
    const box = $("#errors");
    try {
      const { errors } = await api("/api/errors?limit=100");
      if (!errors.length) { box.innerHTML = `<div class="empty">No errors. Nice.</div>`; return; }
      box.innerHTML = errors.map((e) => `
        <div class="err">
          <div><span class="badge error">${esc(e.stage)}</span> ${esc(e.message)}</div>
          <div class="when">${when(e.ts)}${e.link_id ? " · sync " + esc(e.link_id.slice(0, 8)) : ""}</div>
          ${e.detail || Object.keys(e.context || {}).length ? `<details><summary class="muted">details</summary><pre>${esc(JSON.stringify(e.context || {}, null, 2))}\n${esc(e.detail)}</pre></details>` : ""}
        </div>`).join("");
    } catch (err) { box.innerHTML = `<div class="empty">Could not load errors: ${esc(err.message)}</div>`; }
  }
  $("#refresh-errors").onclick = loadErrors;
  $("#clear-errors").onclick = async () => { await api("/api/errors", { method: "DELETE" }); loadErrors(); };

  load().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p class="card">Failed to load: ${esc(e.message)}</p>`); });
})();
