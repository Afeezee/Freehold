"use strict";

/* Freehold — interface.
   Plain ES2020, no framework: this is served from inside the packaged
   executable, so there is no build step to depend on. */

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

/* ── small helpers ──────────────────────────────────────────────────── */
async function api(path, body) {
  const opts = {
    headers: { "X-Freehold-Token": TOKEN, "Content-Type": "application/json" },
  };
  if (body !== undefined) {
    opts.method = "POST";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const doc = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(doc.error || ("Request failed (" + res.status + ")"));
  return doc;
}

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(kid));
  }
  return node;
}

/* Inline SVG rather than an icon font or emoji: scales, inherits colour,
   and carries no download. Every one is aria-hidden; the label is text. */
const ICON = {
  shield: '<path d="M12 3 4 6v5c0 4.5 3.2 8.6 8 10 4.8-1.4 8-5.5 8-10V6z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8.6 11.8 2.3 2.3 4.5-4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
  spark: '<path d="M12 4v8l5 3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  alert: '<path d="M12 4.5 2.8 19.5h18.4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 10v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="16.6" r="1" fill="currentColor"/>',
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h7A1.5 1.5 0 0 1 19 8.5v8A1.5 1.5 0 0 1 17.5 18h-13A1.5 1.5 0 0 1 3 16.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
};

/* SVG lives in its own namespace: document.createElement("svg") produces an
   HTML element that renders as nothing, so these must be created with
   createElementNS. */
function icon(name, size) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("width", size || 18);
  svg.setAttribute("height", size || 18);
  svg.innerHTML = ICON[name] || "";
  return svg;
}

function bytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  for (const u of units) {
    if (v < 1024 || u === "TB") {
      return [u === "B" ? String(Math.round(v)) : v.toFixed(1), u];
    }
    v /= 1024;
  }
}

function bytesText(n) { const [a, b] = bytes(n); return a + " " + b; }

function when(ts) {
  if (!ts) return "never";
  const d = new Date(Number(ts) * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.floor(Date.now() / 1000 - Number(ts));
  if (s < 90) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " minutes ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h === 1 ? "an hour ago" : h + " hours ago";
  const d = Math.floor(h / 24);
  if (d === 1) return "yesterday";
  if (d < 30) return d + " days ago";
  const mo = Math.floor(d / 30);
  return mo === 1 ? "a month ago" : mo + " months ago";
}

function toast(message, bad) {
  const node = el("div", { class: "toast" + (bad ? " bad" : ""), role: "status" },
    message);
  $("toasts").append(node);
  setTimeout(() => node.remove(), bad ? 7000 : 4000);
}

async function guard(fn, label) {
  try { return await fn(); }
  catch (e) { toast((label ? label + ": " : "") + e.message, true); }
}

/* Keeps a button honest while something is in flight. */
async function busy(button, label, fn) {
  const original = button.textContent;
  button.disabled = true;
  button.replaceChildren(el("span", { class: "spin" }), label);
  try { return await fn(); }
  finally { button.disabled = false; button.textContent = original; }
}

/* ── state ──────────────────────────────────────────────────────────── */
const state = {
  tab: "overview",
  health: null,
  drivers: [],
  pickerPath: "",
  pickerTarget: "backup",
  snapshots: [],
  prefix: "",
  picked: new Set(),
  tree: null,
  timers: {},
};

/* ── routing ────────────────────────────────────────────────────────── */
const TABS = ["overview", "backup", "restore", "storage"];

function show(tab) {
  const onboarding = state.health && state.health.state === "setup" &&
    !(state.settings && state.settings["ui.onboarded"] === "yes");
  const target = onboarding && tab === "overview" ? "onboarding" : tab;

  for (const name of TABS.concat(["onboarding"])) {
    const view = $("view-" + name);
    if (view) view.hidden = name !== target;
  }
  for (const link of document.querySelectorAll(".tabs a")) {
    const on = link.dataset.tab === tab;
    link.toggleAttribute("aria-current", on);
    if (on) link.setAttribute("aria-current", "page");
  }
  state.tab = tab;
  if (tab === "restore") loadSnapshots();
  if (tab === "storage") { loadBackends(); loadDevices(); loadCredentials(); }
  if (tab === "backup" || tab === "overview") loadFolders();
}

function route() {
  const tab = (location.hash || "#overview").slice(1);
  show(TABS.includes(tab) ? tab : "overview");
}
window.addEventListener("hashchange", route);

for (const b of document.querySelectorAll("[data-go]")) {
  b.addEventListener("click", () => { location.hash = "#" + b.dataset.go; });
}

/* ── overview ───────────────────────────────────────────────────────── */
async function loadHealth() {
  const h = await api("/api/health");
  state.health = h;
  state.settings = await api("/api/settings");

  const card = $("status-card");
  const map = { ok: "", setup: "is-setup", stale: "is-stale", bad: "is-bad" };
  card.className = "status " + (map[h.state] || "");
  const glyph = { ok: "shield", setup: "spark", stale: "alert", bad: "alert" };
  $("status-icon").replaceChildren(icon(glyph[h.state] || "shield", 22));
  $("status-headline").textContent = h.headline;
  $("status-detail").textContent =
    h.state === "bad"
      ? "Nothing from " + (h.failed_folder || "that folder") + " was stored. " +
        "Check your storage under Storage, then try again."
    : h.state === "ok"
      ? "Last backup " + ago(h.last_backup) + ". " +
        (h.mirrored ? "Kept in " + h.places + " places."
                    : "Kept in one place — add a second for safety.")
    : h.state === "stale"
      ? "Last backup " + ago(h.last_backup) + "."
      : "Follow the steps to get protected.";

  $("facts").replaceChildren(
    fact("Protected", bytes(h.protected_bytes)),
    fact("Copies kept in", [String(h.places), h.places === 1 ? "place" : "places"]),
    fact("Last backup", [ago(h.last_backup), ""]),
  );

  renderKeyBanner(h.key);
  renderOnboarding(h);
  $("updated").textContent = "Updated " + when(Date.now() / 1000);
}

function fact(label, value) {
  const [n, unit] = Array.isArray(value) ? value : [value, ""];
  return el("div", {},
    el("dt", {}, label),
    el("dd", {}, n, unit ? el("span", { class: "unit" }, " " + unit) : null));
}

function renderKeyBanner(key) {
  const slot = $("banner-slot");
  if (!key || !key.exists || key.saved_elsewhere) {
    slot.replaceChildren();
    return;
  }
  slot.replaceChildren(el("div", { class: "banner", role: "status" },
    icon("alert", 20),
    el("div", { class: "banner-body" },
      el("strong", {}, "Save a copy of your encryption key"),
      el("p", {}, "Without this file your backups cannot be recovered by " +
        "anyone, including you. Copy it to a USB stick or a password manager."),
      el("p", {}, el("code", {}, key.path))),
    el("button", {
      class: "btn sm",
      onclick: (ev) => guard(async () => {
        await api("/api/settings", { key: "ui.key_saved", value: "yes" });
        toast("Noted. You can find the key at " + key.path);
        loadHealth();
      }),
    }, "I have saved it")));
}

/* ── onboarding ─────────────────────────────────────────────────────── */
function renderOnboarding(h) {
  $("onboard-keypath").textContent = h.key.path;
  const steps = {
    key: h.key.saved_elsewhere,
    place: h.places > 0,
    folder: h.last_backup > 0,
  };
  let currentFound = false;
  for (const li of document.querySelectorAll("#onboard-steps .step")) {
    const done = steps[li.dataset.step];
    li.classList.toggle("done", !!done);
    li.classList.toggle("current", !done && !currentFound);
    if (!done) currentFound = true;
  }
  $("key-saved-check").checked = !!steps.key;
  if (steps.key && steps.place && steps.folder) {
    guard(async () => {
      await api("/api/settings", { key: "ui.onboarded", value: "yes" });
      state.settings["ui.onboarded"] = "yes";
      show("overview");
    });
  }
}

$("key-saved-check").addEventListener("change", (ev) => guard(async () => {
  await api("/api/settings",
    { key: "ui.key_saved", value: ev.target.checked ? "yes" : "" });
  loadHealth();
}));

$("copy-keypath").addEventListener("click", async () => {
  const text = $("onboard-keypath").textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast("Path copied");
  } catch {
    toast("Could not copy. The path is: " + text, true);
  }
});

$("onboard-add-place").addEventListener("click", () => {
  location.hash = "#storage";
  $("add-backend").hidden = false;
  $("add-backend").querySelector("input[name=name]").focus();
});
$("onboard-pick-folder").addEventListener("click", () => {
  location.hash = "#backup";
  openPicker();
});

/* ── folder picker ──────────────────────────────────────────────────── */
function openPicker() {
  $("picker").hidden = false;
  $("open-picker").setAttribute("aria-expanded", "true");
  browse("");
}

$("open-picker").addEventListener("click", openPicker);
$("picker-cancel").addEventListener("click", () => {
  $("picker").hidden = true;
  $("open-picker").setAttribute("aria-expanded", "false");
});

async function browse(path) {
  const data = await guard(() =>
    api("/api/browse-local?path=" + encodeURIComponent(path)), "Cannot open");
  if (!data) return;
  state.pickerPath = data.path;

  $("picker-shortcuts").replaceChildren(...data.shortcuts.map((s) =>
    el("button", {
      class: "btn sm", type: "button", onclick: () => browse(s.path),
    }, s.label)));

  const crumbs = [];
  if (data.parent) {
    crumbs.push(el("button", { type: "button", onclick: () => browse(data.parent) },
      "↑ up"), el("span", { class: "sep" }, " "));
  }
  crumbs.push(el("span", {}, data.path || "This computer"));
  $("picker-path").replaceChildren(...crumbs);

  $("picker-list").replaceChildren(...(data.dirs.length
    ? data.dirs.map((d) => el("li", {},
      el("button", { type: "button", onclick: () => browse(d.path) },
        icon("folder", 16), d.name)))
    : [el("li", {}, el("p", { class: "empty" },
      data.path ? "No folders inside this one."
                : "Choose a starting point above."))]));

  const choose = $("picker-choose");
  choose.disabled = !data.path;
  choose.textContent = data.path
    ? "Back up " + (data.path.split(/[\\/]/).filter(Boolean).pop() || data.path)
    : "Back up this folder";
}

$("picker-choose").addEventListener("click", (ev) => {
  const path = state.pickerPath;
  if (!path) return;
  $("picker").hidden = true;
  $("picker-hint").textContent = path;
  busy(ev.target, "Starting…", () => guard(async () => {
    await api("/api/backup", { path });
    pollBackups();
  }, "Backup"));
});

/* ── backups ────────────────────────────────────────────────────────── */
function jobCard(j, kind) {
  const running = j.state === "running" || j.state === "queued";
  const title = kind === "backup"
    ? { queued: "Starting…", running: "Backing up…", done: "Backed up",
        failed: "Backup did not finish" }[j.state]
    : { queued: "Starting…", running: "Recovering…", done: "Files recovered",
        failed: "Some files could not be recovered" }[j.state];
  const detail = kind === "backup"
    ? j.files + (j.files === 1 ? " file" : " files") + " protected · " +
      (j.uploaded ? bytesText(j.uploaded) + " sent" : "nothing had changed") +
      (j.dropped ? " · " + j.dropped.toLocaleString() +
        " could NOT be stored" : "")
    : j.restored + (j.restored === 1 ? " file" : " files") + " · " +
      bytesText(j.bytes_written) +
      (j.served_by && j.served_by.length ? " · from " + j.served_by.join(", ") : "");

  const card = el("div", { class: "job " + j.state },
    el("div", { class: "job-head" },
      running ? el("span", { class: "spin" }) : null,
      el("span", { class: "job-title" }, title)),
    el("div", { class: "job-sub" }, kind === "backup" ? j.path : j.dest),
    el("div", { class: "job-sub" }, detail +
      (running && j.current ? " · " + j.current : "")));

  if (j.error) card.append(el("p", { class: "alert error" }, j.error));
  for (const w of (j.warnings || []).slice(0, 3)) {
    card.append(el("div", { class: "job-sub" }, w));
  }
  if (j.renamed && Object.keys(j.renamed).length) {
    card.append(el("div", { class: "job-sub" },
      Object.keys(j.renamed).length + " file(s) already existed, so the " +
      "recovered copies were saved beside them"));
  }
  if (j.unreachable && j.unreachable.length) {
    card.append(el("div", { class: "job-sub" },
      "Could not reach: " + j.unreachable.join(", ")));
  }
  const failures = j.failures || [];
  if (failures.length) {
    card.append(el("ul", {}, failures.slice(0, 10).map((f) =>
      el("li", {}, f.path + " — " + f.reason))));
  }
  for (const e of (j.errors || []).slice(0, 5)) {
    card.append(el("li", { class: "job-sub" }, e));
  }
  return card;
}

async function pollBackups() {
  const data = await guard(() => api("/api/backups"));
  if (!data) return;
  $("backup-jobs").replaceChildren(
    ...data.jobs.slice(0, 3).map((j) => jobCard(j, "backup")));
  const busyNow = data.jobs.some((j) => j.state === "running" || j.state === "queued");
  clearTimeout(state.timers.backup);
  if (busyNow) state.timers.backup = setTimeout(pollBackups, 900);
  else { loadFolders(); loadHealth(); }
}

function folderRow(f, withActions) {
  return el("div", { class: "row-item" },
    icon("folder", 18),
    el("div", { class: "row-main" },
      el("div", { class: "row-title" }, f.path),
      el("div", { class: "row-sub" },
        "Backed up " + ago(f.last_backup) + " · " +
        (f.files || 0).toLocaleString() + " files · " + bytesText(f.size))),
    withActions ? el("div", { class: "row-actions" },
      el("button", {
        class: "btn sm", type: "button",
        onclick: (ev) => busy(ev.target, "Starting…", () => guard(async () => {
          await api("/api/backup", { path: f.path });
          pollBackups();
        })),
      }, "Back up now"),
      el("button", {
        class: "btn sm ghost danger", type: "button",
        onclick: () => guard(async () => {
          if (!confirm("Stop protecting " + f.path +
            "?\n\nBackups already made are kept.")) return;
          await api("/api/folders/forget", { path: f.path });
          loadFolders();
        }),
      }, "Stop")) : null);
}

async function loadFolders() {
  const data = await guard(() => api("/api/folders"));
  if (!data) return;
  const empty = () => el("p", { class: "empty" },
    "No folders yet. Choose one above and Freehold will keep it safe.");
  // Built twice rather than cloned: cloneNode drops event listeners, which
  // would leave the second copy's buttons silently dead.
  for (const [id, actions] of [["folder-list", true],
                               ["overview-folders", false]]) {
    const host = $(id);
    if (!host) continue;
    host.replaceChildren(...(data.folders.length
      ? data.folders.map((f) => folderRow(f, actions))
      : [empty()]));
  }
}

async function loadActivity() {
  const [b, r] = await Promise.all([
    guard(() => api("/api/backups")), guard(() => api("/api/jobs"))]);
  const all = [...((b && b.jobs) || []).map((j) => [j, "backup"]),
               ...((r && r.jobs) || []).map((j) => [j, "restore"])]
    .sort((x, y) => (y[0].started_at || 0) - (x[0].started_at || 0))
    .slice(0, 4);
  $("activity").replaceChildren(...(all.length
    ? all.map(([j, kind]) => jobCard(j, kind))
    : [el("p", { class: "empty" }, "Nothing has run yet.")]));
}

/* ── storage ────────────────────────────────────────────────────────── */
const KIND = {
  local: { name: "A folder, disk or USB drive",
           blurb: "Simplest and fastest. Keep the drive somewhere else if you can." },
  github: { name: "GitHub",
            blurb: "Free private storage. Needs a token from your GitHub account." },
  s3: { name: "Cloudflare R2, Backblaze B2 or other S3 storage",
        blurb: "Around 10 GB free on most providers." },
};

/* Fields people can fill in, instead of key=value strings. */
const FIELDS = {
  local: [{ k: "path", label: "Folder to keep backups in",
            placeholder: "D:\\freehold-backups" }],
  github: [{ k: "owner", label: "Your GitHub username",
             placeholder: "yourname" }],
  s3: [{ k: "endpoint", label: "Endpoint",
         placeholder: "https://account.r2.cloudflarestorage.com" },
       { k: "bucket", label: "Bucket name", placeholder: "freehold" },
       { k: "region", label: "Region", placeholder: "auto" }],
};

async function loadBackends() {
  const data = await guard(() => api("/api/backends?live=1"));
  if (!data) return;
  state.drivers = data.drivers;

  $("backend-cards").replaceChildren(...(data.backends.length
    ? data.backends.map(backendCard)
    : [el("div", { class: "card" }, el("p", { class: "empty" },
        "No storage yet. Add a place to start backing up."))]));

  const sel = $("driver-select");
  if (!sel.options.length) {
    sel.replaceChildren(...data.drivers.map((d) =>
      el("option", { value: d.id }, (KIND[d.id] || {}).name || d.id)));
    sel.addEventListener("change", renderDriverFields);
    renderDriverFields();
  }
}

function renderDriverFields() {
  const id = $("driver-select").value;
  const driver = state.drivers.find((d) => d.id === id) || {};
  const nodes = [];
  if (KIND[id]) nodes.push(el("p", { class: "muted sm" }, KIND[id].blurb));
  for (const f of FIELDS[id] || []) {
    nodes.push(el("label", { class: "field" },
      el("span", {}, f.label),
      el("input", { name: "opt:" + f.k, placeholder: f.placeholder,
                    autocomplete: "off" })));
  }
  for (const s of driver.secrets || []) {
    nodes.push(el("label", { class: "field" },
      el("span", {}, s.label),
      el("input", { type: "password", name: "secret:" + s.name,
                    autocomplete: "off",
                    placeholder: "leave blank if already saved" }),
      el("small", {}, "Kept on this computer. Never written into your backup.")));
  }
  if (!FIELDS[id]) {
    nodes.push(el("label", { class: "field" },
      el("span", {}, "Settings"),
      el("input", { name: "raw", placeholder: "key=value key=value" }),
      el("small", {}, driver.options || "")));
  }
  $("driver-fields").replaceChildren(...nodes);
}

function backendCard(b) {
  const pill = b.error ? el("span", { class: "pill err" }, "problem")
    : !b.enabled ? el("span", { class: "pill paused" }, "paused")
      : el("span", { class: "pill" }, "ready");

  const card = el("div", { class: "card" + (b.enabled ? "" : " off") },
    el("div", { class: "card-head" },
      el("h3", {}, b.name), pill),
    el("div", { class: "kind" }, (KIND[b.driver] || {}).name || b.driver),
    el("dl", {},
      el("div", {}, el("dt", {}, "Holding"),
        el("dd", {}, bytesText(b.bytes))),
      b.quota ? el("div", {}, el("dt", {}, "Room"),
        el("dd", {}, bytesText(b.quota))) : null));

  if (b.quota) {
    card.append(el("div", { class: "meter" },
      el("span", { style: "width:" + Math.min(100, b.bytes / b.quota * 100) + "%" })));
  }
  const repos = (b.detail && b.detail.repos) || [];
  if (repos.length) {
    card.append(el("details", { class: "sub-list" },
      el("summary", { class: "muted sm" }, repos.length + " repositories"),
      ...repos.map((r) => el("div", {},
        el("span", {}, r.name + (r.status === "active" ? "" : " · full")),
        el("span", {}, bytesText(r.bytes))))));
  }
  if (b.error) card.append(el("p", { class: "alert error" }, b.error));

  const note = el("span", { class: "muted sm" });
  card.append(el("div", { class: "card-actions" },
    el("button", {
      class: "btn sm", type: "button",
      onclick: (ev) => busy(ev.target, "Checking…", () => guard(async () => {
        const r = await api("/api/backends/check", { name: b.name });
        note.textContent = r.ok ? "Working" : r.message;
      })),
    }, "Check"),
    el("button", {
      class: "btn sm ghost", type: "button",
      onclick: () => guard(async () => {
        await api("/api/backends/enable", { name: b.name, enabled: !b.enabled });
        loadBackends();
      }),
    }, b.enabled ? "Pause" : "Resume"),
    el("button", {
      class: "btn sm ghost danger", type: "button",
      onclick: () => guard(async () => {
        if (!confirm("Remove " + b.name + "?\n\nAnything stored only here " +
          "cannot be recovered afterwards.")) return;
        await api("/api/backends/remove", { name: b.name });
        toast("Removed " + b.name);
        loadBackends();
      }),
    }, "Remove"),
    note));
  return card;
}

$("toggle-add").addEventListener("click", () => {
  const form = $("add-backend");
  form.hidden = !form.hidden;
  if (!form.hidden) form.querySelector("input[name=name]").focus();
});
$("cancel-add").addEventListener("click", () => { $("add-backend").hidden = true; });

$("add-backend").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const err = $("add-error");
  err.hidden = true;
  const submit = form.querySelector("button[type=submit]");

  busy(submit, "Adding…", async () => {
    const options = {};
    for (const f of form.querySelectorAll("input[name^='opt:']")) {
      if (f.value.trim()) options[f.name.slice(4)] = f.value.trim();
    }
    const raw = form.querySelector("input[name=raw]");
    for (const pair of ((raw && raw.value) || "").trim().split(/\s+/)) {
      const i = pair.indexOf("=");
      if (i > 0) options[pair.slice(0, i)] = pair.slice(i + 1);
    }
    try {
      for (const f of form.querySelectorAll("input[name^='secret:']")) {
        if (f.value.trim()) {
          await api("/api/credentials",
            { name: f.name.slice(7), value: f.value.trim() });
        }
      }
      await api("/api/backends", {
        name: form.name.value, driver: form.driver.value, options,
      });
      form.reset();
      renderDriverFields();
      form.hidden = true;
      toast("Storage added");
      loadBackends();
      loadHealth();
      loadCredentials();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  });
});

async function loadDevices() {
  const data = await guard(() => api("/api/overview"));
  if (!data) return;
  $("device-list").replaceChildren(...(data.devices.length
    ? data.devices.map((d) => el("div", { class: "row-item" },
      el("div", { class: "row-main" },
        el("div", {}, d.name),
        el("div", { class: "row-sub" },
          (d.platform || "unknown system") + " · last backup " +
          ago(d.last_backup) + " · " + d.files.toLocaleString() +
          " files · " + bytesText(d.size)))))
    : [el("p", { class: "empty" }, "No backups yet.")]));
}

async function loadCredentials() {
  const data = await guard(() => api("/api/credentials"));
  if (!data) return;
  const label = { "credentials.json": "saved on this computer",
                  environment: "from your system settings",
                  "not set": "not set" };
  $("cred-list").replaceChildren(...data.credentials.map((c) =>
    el("div", { class: "row-item" },
      el("div", { class: "row-main" },
        el("div", { class: "row-title" }, c.name),
        el("div", { class: "row-sub" }, label[c.source] || c.source)),
      c.source === "credentials.json"
        ? el("button", {
          class: "btn sm ghost danger", type: "button",
          onclick: () => guard(async () => {
            if (!confirm("Forget " + c.name + "?")) return;
            await api("/api/credentials/remove", { name: c.name });
            loadCredentials();
          }),
        }, "Forget")
        : null)));
}

/* ── recovering ─────────────────────────────────────────────────────── */
async function loadSnapshots() {
  const over = await guard(() => api("/api/overview"));
  if (!over) return;
  const sel = $("restore-device");
  if (!sel.options.length || sel.options.length !== over.devices.length) {
    sel.replaceChildren(...over.devices.map((d) =>
      el("option", { value: d.name }, d.name)));
  }
  const device = sel.value;
  if (!device) { $("browse-empty").hidden = false; return; }

  const data = await guard(() =>
    api("/api/snapshots?device=" + encodeURIComponent(device)));
  if (!data) return;
  state.snapshots = data.snapshots;
  $("restore-snapshot").replaceChildren(...data.snapshots.map((s, i) =>
    el("option", { value: s.id },
      (i === 0 ? "Most recent — " : "") + when(s.started_at) +
      " · " + s.file_count + " files")));
  state.prefix = "";
  state.picked.clear();
  if (data.snapshots.length) loadTree();
  else { $("browse-rows").replaceChildren(); $("browse-empty").hidden = false; }
}

async function loadTree() {
  const id = $("restore-snapshot").value;
  if (!id) return;
  const data = await guard(() => api("/api/tree?snapshot=" + id +
    "&prefix=" + encodeURIComponent(state.prefix)));
  if (!data) return;
  state.tree = data;

  const parts = state.prefix ? state.prefix.split("/") : [];
  const crumbs = [el("button", {
    type: "button", onclick: () => { state.prefix = ""; loadTree(); },
  }, data.snapshot.root)];
  parts.forEach((part, i) => {
    crumbs.push(el("span", { class: "sep" }, "/"));
    crumbs.push(el("button", {
      type: "button",
      onclick: () => { state.prefix = parts.slice(0, i + 1).join("/"); loadTree(); },
    }, part));
  });
  $("crumbs").replaceChildren(...crumbs);
  renderRows();
}

function tickbox(path) {
  return el("input", {
    type: "checkbox", checked: state.picked.has(path),
    "aria-label": "Select " + path,
    onchange: (ev) => {
      if (ev.target.checked) state.picked.add(path);
      else state.picked.delete(path);
      updateRestoreNote();
    },
  });
}

function renderRows() {
  const rows = [];
  if (state.prefix) {
    rows.push(el("tr", {}, el("td", { class: "pick" }),
      el("td", { colspan: "3" }, el("button", {
        class: "cell-link", type: "button",
        onclick: () => {
          const p = state.prefix.split("/"); p.pop();
          state.prefix = p.join("/"); loadTree();
        },
      }, "↑ up"))));
  }
  for (const d of state.tree.dirs) {
    rows.push(el("tr", {},
      el("td", { class: "pick" }, tickbox(d.path)),
      el("td", {}, el("button", {
        class: "cell-link", type: "button",
        onclick: () => { state.prefix = d.path; loadTree(); },
      }, d.name + "/")),
      el("td", { class: "n" }, bytesText(d.size)),
      el("td", { class: "muted sm" }, d.files + " files")));
  }
  for (const f of state.tree.files) {
    rows.push(el("tr", {},
      el("td", { class: "pick" }, tickbox(f.path)),
      el("td", { class: "name" }, f.name),
      el("td", { class: "n" }, bytesText(f.size)),
      el("td", { class: "muted sm" }, when(f.mtime))));
  }
  $("browse-rows").replaceChildren(...rows);
  $("browse-empty").hidden = !!(state.tree.dirs.length || state.tree.files.length);
  updateRestoreNote();
}

function updateRestoreNote() {
  const n = state.picked.size;
  $("restore-note").textContent = n
    ? n + " selected" : "nothing ticked — everything will be recovered";
  $("restore-go").textContent = n ? "Recover selected" : "Recover everything";
}

$("pick-all").addEventListener("change", (ev) => {
  if (!state.tree) return;
  for (const row of [...state.tree.dirs, ...state.tree.files]) {
    if (ev.target.checked) state.picked.add(row.path);
    else state.picked.delete(row.path);
  }
  renderRows();
});
$("restore-device").addEventListener("change", loadSnapshots);
$("restore-snapshot").addEventListener("change", () => {
  state.prefix = ""; state.picked.clear(); loadTree();
});

$("restore-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const err = $("restore-error");
  err.hidden = true;
  busy($("restore-go"), "Starting…", async () => {
    try {
      await api("/api/restore", {
        snapshot: Number($("restore-snapshot").value),
        dest: $("restore-dest").value,
        paths: [...state.picked],
        overwrite: $("restore-overwrite").checked,
      });
      pollJobs();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  });
});

async function pollJobs() {
  const data = await guard(() => api("/api/jobs"));
  if (!data) return;
  $("jobs").replaceChildren(...data.jobs.map((j) => jobCard(j, "restore")));
  const busyNow = data.jobs.some((j) => j.state === "running" || j.state === "queued");
  clearTimeout(state.timers.jobs);
  if (busyNow) state.timers.jobs = setTimeout(pollJobs, 1000);
  else loadHealth();
}

/* ── start ──────────────────────────────────────────────────────────── */
$("refresh").addEventListener("click", () => {
  loadHealth(); loadFolders(); loadActivity();
  if (state.tab === "storage") { loadBackends(); loadDevices(); loadCredentials(); }
  if (state.tab === "restore") loadSnapshots();
});

(async function start() {
  try {
    await loadHealth();
    route();
    await Promise.all([loadFolders(), loadBackends(), loadActivity(),
                       pollBackups(), pollJobs()]);
  } catch (e) {
    document.body.prepend(el("div", { class: "wrap" },
      el("p", { class: "alert error" }, "Could not reach Freehold: " + e.message)));
  }
})();
