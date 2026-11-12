"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

async function call(path, body) {
  const opts = {
    headers: { "X-Freehold-Token": TOKEN, "Content-Type": "application/json" },
  };
  if (body !== undefined) {
    opts.method = "POST";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const doc = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(doc.error || ("HTTP " + res.status));
  return doc;
}

function bytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  for (const u of units) {
    if (v < 1024 || u === "TB") {
      return (u === "B" ? v.toFixed(0) : v.toFixed(1)) + " " + u;
    }
    v /= 1024;
  }
}

function when(ts) {
  if (!ts) return "never";
  const d = new Date(Number(ts) * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
    pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function ago(ts) {
  if (!ts) return "never";
  const mins = Math.floor((Date.now() / 1000 - Number(ts)) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + " min ago";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
  const days = Math.floor(hours / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(kid));
  }
  return node;
}

const state = {
  snapshots: [], prefix: "", picked: new Set(), tree: null,
  pickerPath: "", chosen: "",
};

// -- summary --------------------------------------------------------------
async function loadOverview() {
  const data = await call("/api/overview");
  const t = data.totals;
  $("t-protected").textContent = bytes(t.plaintext_bytes);
  $("t-places").textContent = data.backends.length +
    (data.backends.length === 1 ? " place" : " places");
  const latest = data.devices.reduce(
    (best, d) => Math.max(best, d.last_backup || 0), 0);
  $("t-last").textContent = ago(latest);

  $("device-rows").replaceChildren(...data.devices.map((d) => el("tr", {},
    el("td", {}, d.name),
    el("td", {}, d.platform || "—"),
    el("td", {}, when(d.last_backup)),
    el("td", { class: "n" }, d.files.toLocaleString()),
    el("td", { class: "n" }, bytes(d.size)))));
  $("devices-empty").hidden = data.devices.length > 0;

  // No storage configured yet: say so plainly instead of showing an
  // empty page that looks broken.
  $("setup").hidden = data.backends.length > 0;
  $("backup").hidden = data.backends.length === 0;

  const devSel = $("restore-device");
  const keep = devSel.value;
  devSel.replaceChildren(...data.devices.map((d) =>
    el("option", { value: d.name }, d.name)));
  if (keep) devSel.value = keep;
  $("updated").textContent = "Updated " + when(data.generated_at);
}

// -- choosing a folder ----------------------------------------------------
$("pick-folder").addEventListener("click", () => {
  $("picker").hidden = false;
  openFolder("");
});
$("picker-cancel").addEventListener("click", () => {
  $("picker").hidden = true;
});
$("picker-choose").addEventListener("click", () => {
  if (!state.pickerPath) return;
  $("picker").hidden = true;
  state.chosen = state.pickerPath;
  $("chosen").textContent = state.chosen;
  startBackup(state.chosen);
});

async function openFolder(path) {
  const data = await call("/api/browse-local?path=" +
    encodeURIComponent(path));
  state.pickerPath = data.path;

  $("picker-shortcuts").replaceChildren(...data.shortcuts.map((s) =>
    el("button", {
      class: "chip", onclick: () => openFolder(s.path),
    }, s.label)));

  const crumbs = [];
  if (data.parent !== null && data.parent !== undefined) {
    crumbs.push(el("button", { onclick: () => openFolder(data.parent) },
      "↑ up"));
    crumbs.push(el("span", {}, " "));
  }
  crumbs.push(el("strong", { class: "mono" }, data.path || "This computer"));
  $("picker-path").replaceChildren(...crumbs);

  $("picker-list").replaceChildren(...data.dirs.map((d) =>
    el("li", {}, el("button", {
      class: "link", onclick: () => openFolder(d.path),
    }, d.name))));
  $("picker-choose").disabled = !data.path;
  $("picker-choose").textContent = data.path
    ? "Back up " + data.path.split(/[\\/]/).filter(Boolean).pop()
    : "Choose a folder on the left";
}

// -- backing up -----------------------------------------------------------
async function startBackup(path) {
  try {
    await call("/api/backup", { path });
    pollBackups();
  } catch (e) {
    $("backup-jobs").replaceChildren(
      el("p", { class: "error" }, e.message));
  }
}

function backupCard(j) {
  const running = j.state === "running" || j.state === "queued";
  const headline = {
    queued: "Starting…", running: "Backing up…",
    done: "Backed up", failed: "Backup failed",
  }[j.state] || j.state;
  const card = el("div", { class: "job" },
    el("div", { class: "state " + j.state }, headline),
    el("div", { class: "mono" }, j.path),
    el("div", { class: "note" },
      j.files + (j.files === 1 ? " file" : " files") +
      " · " + bytes(j.uploaded) + " sent" +
      (running && j.current ? " · " + j.current : "") +
      (j.state === "done" && j.uploaded === 0
        ? " · nothing had changed" : "")));
  if (j.error) card.append(el("p", { class: "error" }, j.error));
  for (const line of (j.errors || []).slice(0, 5)) {
    card.append(el("p", { class: "error" }, line));
  }
  return card;
}

let backupTimer = null;
async function pollBackups() {
  const data = await call("/api/backups");
  $("backup-jobs").replaceChildren(...data.jobs.slice(0, 4).map(backupCard));
  const busy = data.jobs.some((j) => j.state === "running" ||
    j.state === "queued");
  clearTimeout(backupTimer);
  if (busy) backupTimer = setTimeout(pollBackups, 900);
  else { loadFolders(); loadOverview(); loadBackends(); loadSnapshots(); }
}

async function loadFolders() {
  const data = await call("/api/folders");
  $("folder-rows").replaceChildren(...data.folders.map((f) => el("tr", {},
    el("td", { class: "path" }, f.path),
    el("td", {}, ago(f.last_backup)),
    el("td", { class: "n" }, (f.files || 0).toLocaleString()),
    el("td", { class: "n" }, bytes(f.size)),
    el("td", {},
      el("button", {
        class: "link",
        onclick: () => startBackup(f.path),
      }, "Back up now"),
      " ",
      el("button", {
        class: "link",
        onclick: async () => {
          if (!confirm("Stop tracking " + f.path +
            "? Existing backups are kept.")) return;
          await call("/api/folders/forget", { path: f.path });
          loadFolders();
        },
      }, "Remove")))));
  $("folders-empty").hidden = data.folders.length > 0;
}

// -- where files are kept -------------------------------------------------
function quotaBar(used, quota) {
  if (!quota) return null;
  const pct = Math.min(100, (used / quota) * 100);
  return el("div", { class: "bar", title: pct.toFixed(0) + "% full" },
    el("span", { style: "width:" + pct + "%" }));
}

function repoList(detail) {
  const repos = detail && detail.repos;
  if (!repos || !repos.length) return null;
  return el("div", { class: "repos" }, repos.map((r) =>
    el("div", { class: r.status === "sealed" ? "sealed" : "" },
      el("span", {}, r.name + (r.status === "active" ? "" : " · full")),
      el("span", {}, bytes(r.bytes)))));
}

const PLACE_NAMES = {
  local: "A folder on a disk", github: "GitHub", s3: "Cloud storage",
};

function backendCard(b) {
  const card = el("div", { class: "card" + (b.enabled ? "" : " off") },
    el("h3", {}, b.name),
    el("div", { class: "driver" },
      (PLACE_NAMES[b.driver] || b.driver) +
      (b.enabled ? "" : " · paused")),
    el("dl", {},
      el("dt", {}, "Holding"), el("dd", {}, bytes(b.bytes)),
      b.quota ? el("dt", {}, "Room") : null,
      b.quota ? el("dd", {}, bytes(b.quota)) : null));
  const bar = quotaBar(b.bytes, b.quota);
  if (bar) card.append(bar);
  const repos = repoList(b.detail);
  if (repos) card.append(repos);
  if (b.error) card.append(el("p", { class: "error" }, b.error));

  const status = el("span", { class: "note" });
  card.append(el("div", { class: "actions" },
    el("button", {
      class: "link",
      onclick: async () => {
        status.textContent = "checking…";
        try {
          const r = await call("/api/backends/check", { name: b.name });
          status.textContent = r.ok ? "working" : r.message;
        } catch (e) { status.textContent = e.message; }
      },
    }, "Check"),
    el("button", {
      class: "link",
      onclick: async () => {
        await call("/api/backends/enable",
          { name: b.name, enabled: !b.enabled });
        loadBackends();
      },
    }, b.enabled ? "Pause" : "Resume"),
    el("button", {
      class: "link",
      onclick: async () => {
        if (!confirm("Remove " + b.name +
          "? Anything stored only there cannot be recovered.")) return;
        try { await call("/api/backends/remove", { name: b.name }); }
        catch (e) { alert(e.message); }
        loadBackends();
      },
    }, "Remove"),
    status));
  return card;
}

async function loadBackends() {
  const data = await call("/api/backends?live=1");
  const cards = data.backends.map(backendCard);
  $("backend-cards").replaceChildren(...(cards.length ? cards :
    [el("div", { class: "card" },
      el("p", { class: "empty" }, "Nowhere to store backups yet."))]));

  const sel = $("driver-select");
  if (!sel.options.length) {
    sel.replaceChildren(...data.drivers.map((d) =>
      el("option", { value: d.id }, PLACE_NAMES[d.id] || d.id)));
    const help = () => {
      const d = data.drivers.find((x) => x.id === sel.value);
      $("driver-help").textContent = d ? d.options : "";
      $("secret-fields").replaceChildren(...((d && d.secrets) || []).map((s) =>
        el("label", { class: "wide" }, s.label,
          el("input", {
            type: "password", name: "secret:" + s.name,
            autocomplete: "off", placeholder: "leave blank if already saved",
          }),
          el("small", {}, "kept on this computer only"))));
    };
    sel.addEventListener("change", help);
    help();
  }
  loadCredentials();
}

async function loadCredentials() {
  const data = await call("/api/credentials");
  $("cred-rows").replaceChildren(...data.credentials.map((c) => el("tr", {},
    el("td", { class: "mono" }, c.name),
    el("td", { class: c.source === "not set" ? "note" : "" },
      c.source === "credentials.json" ? "saved on this computer"
        : c.source === "environment" ? "from your system settings"
          : "not set"),
    el("td", {}, c.source === "credentials.json"
      ? el("button", {
        class: "link",
        onclick: async () => {
          if (!confirm("Forget " + c.name + "?")) return;
          await call("/api/credentials/remove", { name: c.name });
          loadCredentials();
        },
      }, "Forget")
      : null))));
}

$("setup-add").addEventListener("click", () => {
  $("add-backend").hidden = false;
  $("backends").scrollIntoView({ behavior: "smooth" });
});
$("toggle-add").addEventListener("click", () => {
  $("add-backend").hidden = !$("add-backend").hidden;
});
$("cancel-add").addEventListener("click", () => {
  $("add-backend").hidden = true;
});
$("add-backend").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const options = {};
  for (const pair of (form.options.value || "").trim().split(/\s+/)) {
    if (!pair) continue;
    const i = pair.indexOf("=");
    if (i < 0) continue;
    options[pair.slice(0, i)] = pair.slice(i + 1);
  }
  const err = $("add-error");
  err.hidden = true;
  try {
    for (const field of form.querySelectorAll("input[name^='secret:']")) {
      if (field.value.trim()) {
        await call("/api/credentials", {
          name: field.name.slice(7), value: field.value.trim(),
        });
      }
    }
    await call("/api/backends", {
      name: form.name.value, driver: form.driver.value, options,
    });
    form.reset();
    $("add-backend").hidden = true;
    loadBackends();
    loadOverview();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

// -- getting files back ---------------------------------------------------
async function loadSnapshots() {
  const device = $("restore-device").value;
  if (!device) return;
  const data = await call("/api/snapshots?device=" +
    encodeURIComponent(device));
  state.snapshots = data.snapshots;
  const sel = $("restore-snapshot");
  sel.replaceChildren(...state.snapshots.map((s, i) =>
    el("option", { value: s.id },
      (i === 0 ? "Most recent · " : "") + when(s.started_at) +
      " · " + s.file_count + " files")));
  if (state.snapshots.length) {
    state.prefix = "";
    state.picked.clear();
    loadTree();
  } else {
    $("browse-rows").replaceChildren();
    $("browse-empty").hidden = false;
  }
}

async function loadTree() {
  const snapshot = $("restore-snapshot").value;
  if (!snapshot) return;
  const data = await call("/api/tree?snapshot=" + snapshot +
    "&prefix=" + encodeURIComponent(state.prefix));
  state.tree = data;
  renderCrumbs();
  renderRows();
}

function renderCrumbs() {
  const parts = state.prefix ? state.prefix.split("/") : [];
  const nodes = [el("button", {
    onclick: () => { state.prefix = ""; loadTree(); },
  }, state.tree.snapshot.root)];
  parts.forEach((part, i) => {
    nodes.push(el("span", {}, "/"));
    nodes.push(el("button", {
      onclick: () => {
        state.prefix = parts.slice(0, i + 1).join("/");
        loadTree();
      },
    }, part));
  });
  $("crumbs").replaceChildren(...nodes);
}

function renderRows() {
  const rows = [];
  if (state.prefix) {
    rows.push(el("tr", {},
      el("td", { class: "pick" }),
      el("td", { colspan: "3" }, el("button", {
        class: "link",
        onclick: () => {
          const parts = state.prefix.split("/");
          parts.pop();
          state.prefix = parts.join("/");
          loadTree();
        },
      }, "↑ up"))));
  }
  for (const d of state.tree.dirs) {
    rows.push(el("tr", {},
      el("td", { class: "pick" }, el("input", {
        type: "checkbox",
        checked: state.picked.has(d.path) ? "checked" : null,
        onchange: (ev) => {
          ev.target.checked ? state.picked.add(d.path)
            : state.picked.delete(d.path);
          updateNote();
        },
      })),
      el("td", {}, el("button", {
        class: "link",
        onclick: () => { state.prefix = d.path; loadTree(); },
      }, d.name + "/")),
      el("td", { class: "n" }, bytes(d.size)),
      el("td", { class: "note" }, d.files + " files")));
  }
  for (const f of state.tree.files) {
    rows.push(el("tr", {},
      el("td", { class: "pick" }, el("input", {
        type: "checkbox",
        checked: state.picked.has(f.path) ? "checked" : null,
        onchange: (ev) => {
          ev.target.checked ? state.picked.add(f.path)
            : state.picked.delete(f.path);
          updateNote();
        },
      })),
      el("td", { class: "path" }, f.name),
      el("td", { class: "n" }, bytes(f.size)),
      el("td", { class: "note" }, when(f.mtime))));
  }
  $("browse-rows").replaceChildren(...rows);
  const empty = !state.tree.dirs.length && !state.tree.files.length;
  $("browse-empty").hidden = !empty;
  updateNote();
}

function updateNote() {
  const n = state.picked.size;
  $("restore-note").textContent = n
    ? n + " selected" : "nothing ticked — you will get everything";
  $("restore-go").textContent = n ? "Get these back" : "Get everything back";
}

$("pick-all").addEventListener("change", (ev) => {
  if (!state.tree) return;
  for (const row of [...state.tree.dirs, ...state.tree.files]) {
    ev.target.checked ? state.picked.add(row.path)
      : state.picked.delete(row.path);
  }
  renderRows();
});
$("restore-device").addEventListener("change", loadSnapshots);
$("restore-snapshot").addEventListener("change", () => {
  state.prefix = "";
  state.picked.clear();
  loadTree();
});

$("restore-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("restore-error");
  err.hidden = true;
  try {
    await call("/api/restore", {
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

let jobTimer = null;

function jobCard(j) {
  const headline = {
    queued: "Starting…", running: "Getting files back…",
    done: "Files recovered", failed: "Some files could not be recovered",
  }[j.state] || j.state;
  const card = el("div", { class: "job" },
    el("div", { class: "state " + j.state }, headline),
    el("div", { class: "mono" }, j.dest),
    el("div", { class: "note" },
      j.restored + (j.restored === 1 ? " file" : " files") + " · " +
      bytes(j.bytes_written) +
      (j.served_by && j.served_by.length
        ? " · from " + j.served_by.join(", ") : "") +
      (j.current ? " · " + j.current : "")));
  if (Object.keys(j.renamed).length) {
    card.append(el("div", { class: "note" },
      Object.keys(j.renamed).length +
      " file(s) already existed, so the recovered copies were saved next" +
      " to them"));
  }
  if (j.unreachable.length) {
    card.append(el("div", { class: "error" },
      "could not reach: " + j.unreachable.join(", ")));
  }
  if (j.error) card.append(el("p", { class: "error" }, j.error));
  if (j.failures.length) {
    card.append(el("ul", {}, j.failures.slice(0, 20).map((f) =>
      el("li", {}, f.path + " — " + f.reason))));
  }
  return card;
}

async function pollJobs() {
  const data = await call("/api/jobs");
  $("jobs").replaceChildren(...data.jobs.map(jobCard));
  const busy = data.jobs.some((j) => j.state === "running" ||
    j.state === "queued");
  clearTimeout(jobTimer);
  if (busy) jobTimer = setTimeout(pollJobs, 1000);
  else loadOverview();
}

$("refresh").addEventListener("click", () => {
  loadOverview(); loadBackends(); loadFolders(); loadSnapshots();
  pollBackups(); pollJobs();
});

(async function init() {
  try {
    await loadOverview();
    await loadBackends();
    await loadFolders();
    await loadSnapshots();
    await pollBackups();
    await pollJobs();
  } catch (e) {
    document.body.prepend(el("p", { class: "error", style: "padding:16px" },
      "Could not reach Freehold: " + e.message));
  }
})();
