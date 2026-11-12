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
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
    pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
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

const state = { snapshots: [], prefix: "", picked: new Set(), tree: null };

// -- overview -------------------------------------------------------------
async function loadOverview() {
  const data = await call("/api/overview");
  const t = data.totals;
  $("t-stored").textContent = bytes(t.encrypted_bytes);
  $("t-chunks").textContent = t.chunks.toLocaleString();
  $("t-dedup").textContent = t.plaintext_bytes
    ? (t.encrypted_bytes / t.plaintext_bytes).toFixed(2) + "×" : "—";
  $("t-backends").textContent = String(data.backends.length);

  const rows = $("device-rows");
  rows.replaceChildren(...data.devices.map((d) => el("tr", {},
    el("td", {}, d.name),
    el("td", {}, d.platform || "—"),
    el("td", {}, when(d.last_backup)),
    el("td", { class: "n" }, d.files.toLocaleString()),
    el("td", { class: "n" }, bytes(d.size)))));
  $("devices-empty").hidden = data.devices.length > 0;

  $("folder-rows").replaceChildren(...data.folders.map((f) => el("tr", {},
    el("td", { class: "path" }, f.root),
    el("td", {}, f.device),
    el("td", {}, when(f.last_backup)),
    el("td", { class: "n" }, f.files.toLocaleString()),
    el("td", { class: "n" }, bytes(f.size)))));

  const devSel = $("restore-device");
  const keep = devSel.value;
  devSel.replaceChildren(...data.devices.map((d) =>
    el("option", { value: d.name }, d.name)));
  if (keep) devSel.value = keep;
  $("updated").textContent = "Updated " + when(data.generated_at);
}

// -- backends -------------------------------------------------------------
function quotaBar(used, quota) {
  if (!quota) return null;
  const pct = Math.min(100, (used / quota) * 100);
  return el("div", { class: "bar", title: pct.toFixed(0) + "% of quota" },
    el("span", { style: "width:" + pct + "%" }));
}

function repoList(detail) {
  const repos = detail && detail.repos;
  if (!repos || !repos.length) return null;
  return el("div", { class: "repos" }, repos.map((r) =>
    el("div", { class: r.status === "sealed" ? "sealed" : "" },
      el("span", {}, r.name + (r.status === "active" ? "" : " · sealed")),
      el("span", {}, bytes(r.bytes)))));
}

function backendCard(b) {
  const card = el("div", { class: "card" + (b.enabled ? "" : " off") },
    el("h3", {}, b.name),
    el("div", { class: "driver" }, b.driver + (b.enabled ? "" : " · disabled")),
    el("dl", {},
      el("dt", {}, "Chunks"), el("dd", {}, b.chunks.toLocaleString()),
      el("dt", {}, "Stored"), el("dd", {}, bytes(b.bytes)),
      b.quota ? el("dt", {}, "Quota") : null,
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
          status.textContent = r.ok ? "reachable" : r.message;
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
    }, b.enabled ? "Disable" : "Enable"),
    el("button", {
      class: "link",
      onclick: async () => {
        if (!confirm("Remove backend " + b.name +
          "? Chunks stored only there become unrecoverable.")) return;
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
      el("p", { class: "empty" }, "No backends yet."))]));

  const sel = $("driver-select");
  if (!sel.options.length) {
    sel.replaceChildren(...data.drivers.map((d) =>
      el("option", { value: d.id }, d.id)));
    const help = () => {
      const d = data.drivers.find((x) => x.id === sel.value);
      $("driver-help").textContent = d ? d.options : "";
    };
    sel.addEventListener("change", help);
    help();
  }
  loadOverview();
}

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
    await call("/api/backends", {
      name: form.name.value, driver: form.driver.value, options,
    });
    form.reset();
    $("add-backend").hidden = true;
    loadBackends();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

// -- restore browser ------------------------------------------------------
async function loadSnapshots() {
  const device = $("restore-device").value;
  if (!device) return;
  const data = await call("/api/snapshots?device=" + encodeURIComponent(device));
  state.snapshots = data.snapshots;
  const sel = $("restore-snapshot");
  sel.replaceChildren(...state.snapshots.map((s) =>
    el("option", { value: s.id },
      when(s.started_at) + " · " + s.root + " · " +
      s.file_count + " files")));
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
  if (!$("restore-dest").value) {
    $("restore-dest").placeholder = "folder to restore into";
  }
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
    ? n + " selected" : "nothing selected — restores the whole snapshot";
  $("restore-go").textContent = n ? "Restore selection" : "Restore everything";
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

// -- jobs -----------------------------------------------------------------
let jobTimer = null;

function jobCard(j) {
  const card = el("div", { class: "job" },
    el("div", { class: "state " + j.state }, j.state),
    el("div", { class: "path mono" },
      "snapshot " + j.snapshot_id + " → " + j.dest),
    el("div", { class: "note" },
      (j.paths.length ? j.paths.length + " selected path(s)"
        : "whole snapshot") + " · " +
      j.restored + " files · " + bytes(j.bytes_written) +
      (j.served_by && j.served_by.length
        ? " · from " + j.served_by.join(", ") : "") +
      (j.current ? " · " + j.current : "")));
  if (Object.keys(j.renamed).length) {
    card.append(el("div", { class: "note" },
      Object.keys(j.renamed).length +
      " existing file(s) kept; restored copies written beside them"));
  }
  if (j.unreachable.length) {
    card.append(el("div", { class: "error" },
      "unreachable: " + j.unreachable.join(", ")));
  }
  if (j.error) card.append(el("p", { class: "error" }, j.error));
  if (j.failures.length) {
    card.append(el("ul", {}, j.failures.slice(0, 20).map((f) =>
      el("li", {}, f.path + " — " + f.reason +
        (f.chunk ? " [chunk " + f.chunk.slice(0, 12) + "]" : "") +
        (f.backends.length ? " [tried " + f.backends.join(", ") + "]" : "")))));
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
  loadBackends();
  loadSnapshots();
  pollJobs();
});

(async function init() {
  try {
    await loadOverview();
    await loadBackends();
    await loadSnapshots();
    await pollJobs();
  } catch (e) {
    document.body.prepend(el("p", { class: "error", style: "padding:16px" },
      "Could not reach the dashboard API: " + e.message));
  }
})();
