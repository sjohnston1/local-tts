// Library tab: real folders on disk, uploads, YouTube downloads, search. Data lives in SQLite on the server.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    tree: $("tree"), crumbs: $("crumbs"), items: $("items"), empty: $("libEmpty"), root: $("libRoot"),
    newFolder: $("newFolder"), rename: $("renameFolder"), move: $("moveFolder"), del: $("deleteFolder"),
    folderActions: $("folderActions"), upload: $("uploadBtn"), uploadInput: $("uploadInput"),
    ytUrl: $("ytUrl"), ytAdd: $("ytAdd"), search: $("libSearch"), hits: $("hits"),
    changeRoot: $("changeRoot"), rescan: $("rescan"), openRoot: $("openRoot"),
    dialog: $("moveDialog"), dialogTitle: $("moveTitle"), dialogTarget: $("moveTarget"),
  };
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  const L = { root: "", folders: [], items: [], current: store.get("lib.folder", ""), listeners: [], timer: null };

  const ICON = {
    folder: '<svg viewBox="0 0 24 24"><path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg>',
    home: '<svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>',
    video: '<svg viewBox="0 0 24 24"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg>',
    document: '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
  };

  function snack(msg) {
    const s = $("snack"); s.textContent = msg; s.classList.add("show");
    clearTimeout(snack.t); snack.t = setTimeout(() => s.classList.remove("show"), 5000);
  }
  async function api(path, opts = {}) {
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers = { "Content-Type": "application/json" };
      delete opts.json;
    }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch {}
    if (!r.ok) throw new Error(data?.detail || r.statusText);
    return data;
  }
  const act = (fn) => async (...a) => { try { await fn(...a); } catch (e) { snack(e.message); } };
  const fmtTime = (t) => {
    t = Math.floor(t || 0);
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = String(t % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  };
  const fmtSize = (b) => !b ? "" : b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : b > 1e6 ? (b / 1e6).toFixed(0) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
  const leaf = (p) => p.split("/").pop();
  const parent = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const STATUS = { queued: "Waiting…", downloading: "Downloading", transcribing: "Transcribing", extracting: "Reading",
                   ready: "", error: "Failed", missing: "Missing on disk" };

  // ---------- data ----------
  async function refresh() {
    try {
      const d = await api("/api/library");
      L.root = d.root; L.folders = d.folders; L.items = d.items;
      if (L.current && !L.folders.includes(L.current)) L.current = "";
      render();
      L.listeners.forEach((f) => f());
    } catch (e) { ui.root.textContent = "Server offline"; }
    clearTimeout(L.timer);
    const active = L.items.some((i) => ["queued", "downloading", "transcribing", "extracting"].includes(i.status));
    L.timer = setTimeout(refresh, active ? 1500 : 10000);
  }

  // ---------- rendering ----------
  function render() {
    ui.root.textContent = L.root;
    // folder tree
    ui.tree.innerHTML = "";
    const all = ["", ...L.folders];
    for (const f of all) {
      const b = document.createElement("button");
      const depth = f ? f.split("/").length : 0;
      const count = L.items.filter((i) => i.folder === f).length;
      b.innerHTML = (f ? ICON.folder : ICON.home) + `<span></span>`;
      b.lastChild.textContent = (f ? leaf(f) : "Library") + (count ? `  (${count})` : "");
      b.style.paddingLeft = 10 + depth * 16 + "px";
      b.classList.toggle("sel", f === L.current);
      b.onclick = () => openFolder(f);
      // drop items onto folders to move them
      b.ondragover = (e) => { if (e.dataTransfer.types.includes("text/item")) { e.preventDefault(); b.classList.add("drop"); } };
      b.ondragleave = () => b.classList.remove("drop");
      b.ondrop = act(async (e) => {
        b.classList.remove("drop");
        const id = e.dataTransfer.getData("text/item");
        if (!id) return;
        e.preventDefault(); e.stopPropagation();
        await api(`/api/items/${id}`, { method: "PATCH", json: { folder: f } });
        await refresh();
      });
      ui.tree.append(b);
    }
    ui.folderActions.classList.toggle("hidden", !L.current);
    // breadcrumbs
    ui.crumbs.innerHTML = "";
    const parts = L.current ? L.current.split("/") : [];
    const home = document.createElement("a"); home.textContent = "Library"; home.onclick = () => openFolder("");
    ui.crumbs.append(home);
    parts.forEach((p, i) => {
      ui.crumbs.append(" / ");
      const a = document.createElement("a"); a.textContent = p;
      const path = parts.slice(0, i + 1).join("/");
      a.onclick = () => openFolder(path);
      ui.crumbs.append(a);
    });
    // subfolders + items in this folder
    ui.items.innerHTML = "";
    const subs = L.folders.filter((f) => parent(f) === L.current);
    for (const f of subs) {
      const row = el(`<div class="it"><span>${ICON.folder}</span><div><div class="t"></div><div class="m"></div></div><div class="a"></div></div>`);
      row.querySelector(".t").textContent = leaf(f);
      const n = L.items.filter((i) => i.folder === f || i.folder.startsWith(f + "/")).length;
      row.querySelector(".m").textContent = `Folder · ${n} item${n === 1 ? "" : "s"}`;
      row.style.cursor = "pointer";
      row.onclick = () => openFolder(f);
      ui.items.append(row);
    }
    const here = L.items.filter((i) => i.folder === L.current);
    for (const it of here) ui.items.append(itemRow(it));
    ui.empty.classList.toggle("hidden", subs.length + here.length > 0);
  }
  function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }

  function itemRow(it) {
    const row = el(`<div class="it" draggable="true"><span>${ICON[it.kind]}</span><div><div class="t"></div><div class="m"></div></div><div class="a"></div></div>`);
    row.querySelector(".t").textContent = it.title;
    row.title = it.source_url ? `${it.rel_path}\nFrom ${it.source_url}` : it.rel_path;
    row.ondragstart = (e) => e.dataTransfer.setData("text/item", String(it.id));
    const meta = row.querySelector(".m");
    const bits = [];
    if (it.duration) bits.push(fmtTime(it.duration));
    if (it.size) bits.push(fmtSize(it.size));
    if (it.source_url) bits.push("YouTube");
    meta.textContent = bits.join(" · ");
    if (STATUS[it.status]) {
      const s = document.createElement("span");
      s.textContent = STATUS[it.status];
      if (it.status === "error") { s.className = "err"; s.title = it.error || ""; s.textContent += ": " + (it.error || "").slice(0, 80); }
      if (it.status === "missing") s.className = "err";
      meta.append(s);
      if (["downloading", "transcribing"].includes(it.status)) {
        const p = document.createElement("progress"); p.max = 1; p.value = it.progress || 0; meta.append(p);
      }
    }
    const a = row.querySelector(".a");
    const btn = (label, fn, title) => { const b = document.createElement("button"); b.textContent = label; if (title) b.title = title; b.onclick = act(fn); a.append(b); };
    const hasFile = !it.rel_path.split("/").pop().startsWith(".pending-") && it.status !== "missing";
    if (it.kind === "video" && hasFile) btn("Watch", () => watch(it.id), "Watch in the Talk tab and ask questions");
    if (it.kind === "document" && it.status === "ready") btn("Ask", () => {
      window.setMode("talk");
      window.talk?.focusDoc({ title: it.title, itemId: it.id });
    }, "Ask questions about this file in the Talk tab");
    if (it.kind === "document" && it.status === "ready") btn("Read aloud", async () => {
      const d = await api(`/api/items/${it.id}/text`);
      window.readAloud(d.text, it.title);
    });
    if (it.status === "error") btn("Retry", async () => { await api(`/api/items/${it.id}/retry`, { method: "POST" }); refresh(); });
    if (hasFile) btn("Rename", async () => {
      const t = prompt("New name:", it.title);
      if (!t || t === it.title) return;
      await api(`/api/items/${it.id}`, { method: "PATCH", json: { title: t } });
      refresh();
    });
    if (hasFile) btn("Move", async () => {
      const dest = await pickFolder(`Move "${it.title}" to…`, [it.folder]);
      if (dest === null) return;
      await api(`/api/items/${it.id}`, { method: "PATCH", json: { folder: dest } });
      refresh();
    });
    btn("Delete", async () => {
      if (!confirm(`Delete "${it.title}"? This deletes the file from your PC too.`)) return;
      await api(`/api/items/${it.id}`, { method: "DELETE" });
      refresh();
    });
    return row;
  }

  function watch(id) {
    window.setMode("talk");
    window.videoCtx?.openItem(id);
  }

  function openFolder(f) {
    L.current = f; store.set("lib.folder", f);
    ui.hits.innerHTML = ""; ui.search.value = "";
    render();
  }

  // folder picker dialog (returns folder path, "" for top level, or null if cancelled)
  function pickFolder(title, exclude = [], excludeInside = null) {
    ui.dialogTitle.textContent = title;
    ui.dialogTarget.innerHTML = "";
    for (const f of ["", ...L.folders]) {
      if (exclude.includes(f)) continue;
      if (excludeInside && (f === excludeInside || f.startsWith(excludeInside + "/"))) continue;
      ui.dialogTarget.append(new Option(f ? "Library / " + f.split("/").join(" / ") : "Library (top level)", f));
    }
    if (!ui.dialogTarget.options.length) { snack("There's no other folder to move it to."); return Promise.resolve(null); }
    return new Promise((resolve) => {
      ui.dialog.onclose = () => resolve(ui.dialog.returnValue === "ok" ? ui.dialogTarget.value : null);
      ui.dialog.returnValue = "";
      ui.dialog.showModal();
    });
  }

  // ---------- folder actions ----------
  ui.newFolder.onclick = act(async () => {
    const name = prompt(`New folder inside ${L.current ? '"' + leaf(L.current) + '"' : "the library"}:`);
    if (!name?.trim()) return;
    const d = await api("/api/folders", { method: "POST", json: { path: (L.current ? L.current + "/" : "") + name.trim() } });
    await refresh(); openFolder(d.path);
  });
  ui.rename.onclick = act(async () => {
    const name = prompt("Rename folder:", leaf(L.current));
    if (!name?.trim() || name.trim() === leaf(L.current)) return;
    const p = parent(L.current);
    const d = await api("/api/folders/move", { method: "POST", json: { src: L.current, dst: (p ? p + "/" : "") + name.trim() } });
    L.current = d.path; await refresh(); openFolder(d.path);
  });
  ui.move.onclick = act(async () => {
    const dest = await pickFolder(`Move "${leaf(L.current)}" into…`, [parent(L.current)], L.current);
    if (dest === null) return;
    const d = await api("/api/folders/move", { method: "POST", json: { src: L.current, dst: (dest ? dest + "/" : "") + leaf(L.current) } });
    L.current = d.path; await refresh(); openFolder(d.path);
  });
  ui.del.onclick = act(async () => {
    const n = L.items.filter((i) => i.folder === L.current || i.folder.startsWith(L.current + "/")).length;
    const msg = n ? `Delete "${leaf(L.current)}" and the ${n} file${n === 1 ? "" : "s"} in it? They're deleted from your PC too.`
                  : `Delete the empty folder "${leaf(L.current)}"?`;
    if (!confirm(msg)) return;
    await api(`/api/folders?path=${encodeURIComponent(L.current)}&recursive=true`, { method: "DELETE" });
    L.current = parent(L.current); await refresh();
  });
  ui.changeRoot.onclick = act(async () => {
    const path = prompt("Move the whole library to this folder (for example D:\\Learning\\Library).\n" +
                        "Everything is moved there, and files already in that folder are added.", L.root);
    if (!path?.trim() || path.trim() === L.root) return;
    snack("Moving your library…");
    const d = await api("/api/library/location", { method: "POST", json: { path: path.trim() } });
    snack(`Library is now in ${d.root}`);
    refresh();
  });
  ui.rescan.onclick = act(async () => {
    const d = await api("/api/library/rescan", { method: "POST" });
    snack(`Rescan: ${d.added} new, ${d.moved} moved, ${d.missing} missing, ${d.found} found again.`);
    refresh();
  });
  ui.openRoot.onclick = act(() => api(`/api/library/open?path=${encodeURIComponent(L.current)}`, { method: "POST" }));

  // ---------- adding things ----------
  async function upload(files) {
    const folder = L.current;
    for (const f of files) {
      snack(`Uploading ${f.name}…`);
      try {
        await api(`/api/upload?name=${encodeURIComponent(f.name)}&folder=${encodeURIComponent(folder)}`, { method: "PUT", body: f });
      } catch (e) { snack(`${f.name}: ${e.message}`); }
      refresh();
    }
    snack(files.length > 1 ? `Added ${files.length} files` : `Added ${files[0].name}`);
  }
  ui.upload.onclick = () => ui.uploadInput.click();
  ui.uploadInput.onchange = (e) => { const fs = [...e.target.files]; e.target.value = ""; if (fs.length) upload(fs); };
  ui.ytAdd.onclick = act(async () => {
    const url = ui.ytUrl.value.trim();
    if (!url) return;
    await api("/api/youtube", { method: "POST", json: { url, folder: L.current } });
    ui.ytUrl.value = "";
    snack("Downloading. It'll be transcribed automatically when it's done.");
    refresh();
  });
  ui.ytUrl.onkeydown = (e) => { if (e.key === "Enter") ui.ytAdd.click(); };

  // ---------- search ----------
  let searchTimer;
  ui.search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 300); };
  async function runSearch() {
    const q = ui.search.value.trim();
    ui.hits.innerHTML = "";
    if (!q) return;
    const hits = await api(`/api/search?q=${encodeURIComponent(q)}&folder=${encodeURIComponent(L.current)}`).catch(() => []);
    if (!hits.length) { ui.hits.append(el(`<p class="muted" style="margin:0">No matches.</p>`)); return; }
    for (const h of hits) {
      const d = el(`<div class="hit"><b></b> <span class="muted"></span><p></p></div>`);
      d.querySelector("b").textContent = h.title;
      d.querySelector("span").textContent = [h.folder, h.loc].filter(Boolean).join(" · ");
      d.querySelector("p").textContent = h.text.length > 260 ? h.text.slice(0, 260) + "…" : h.text;
      d.onclick = () => {
        if (h.kind === "video") { window.setMode("talk"); window.videoCtx?.openItem(h.item_id, h.start); }
        else api(`/api/items/${h.item_id}/text`).then((t) => window.readAloud(t.text, h.title)).catch((e) => snack(e.message));
      };
      ui.hits.append(d);
    }
  }

  window.lib = {
    refresh, upload,
    get folders() { return L.folders; },
    get items() { return L.items; },
    onChange(f) { L.listeners.push(f); },
  };
  refresh();
})();
