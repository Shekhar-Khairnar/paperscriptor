// PaperScriptor UI: home page (write a paper, papers list) and the editor (files, source, PDF, chat).
(() => {
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };
  const STEPS = [
    ["outline", "Outline"],
    ["literature", "Literature review"],
    ["writing", "Section writing"],
    ["refining", "Review and refine"],
    ["checking", "Fact check"],
    ["packaging", "Package LaTeX"],
  ];
  const ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
  const ICON_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  const ICON_DL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

  let figures = []; // {file, caption, url}
  let jobId = null, logCount = 0, pollTimer = null, startedAt = 0, elapsedTimer = null;

  // ---------- init ----------
  async function init() {
    const cfg = await fetch("/api/config").then(r => r.json());
    $("template").innerHTML = cfg.templates.map(t => `<option value="${t.id}">${t.label}</option>`).join("")
      + `<option value="__custom__">My own LaTeX template…</option>`;
    $("writer_model").value = store.get("writer_model") || cfg.default_writer;
    if (OLD_DEFAULTS.includes(store.get("search_model"))) store.del("search_model");  // forget an old default
    $("search_model").value = store.get("search_model") || cfg.default_search;
    $("cutoff").value = store.get("cutoff") || cfg.default_cutoff;
    $("envHint").hidden = !cfg.env_key;
    // Old servers don't send this flag at all; the editor endpoints they lack are a sure sign too
    $("restartBanner").hidden = !cfg.restart_needed && "restart_needed" in cfg;
    const savedKey = store.get("api_key");
    if (savedKey) { $("api_key").value = savedKey; $("remember").checked = true; }
    for (const id of DRAFT_FIELDS) {
      const v = store.get("draft_" + id); if (v) $(id).value = v;
    }
    for (const id of ["use_literature", "use_web"]) {
      const v = store.get("draft_" + id); if (v !== null) $(id).checked = v === "1";
    }
    syncWebToggle();
    syncTemplate();
    fetch("/api/models").then(r => r.json()).then(d => {
      modelChoices = d.models;
      ["writer_model", "search_model", "edModel"].forEach(modelPicker);
      if (d.local && d.local.length) {
        $("localHint").textContent = `Local models found: ${d.local.slice(0, 3).join(", ")}${d.local.length > 3 ? "…" : ""}`;
      }
    }).catch(() => {});
    defaultModel = cfg.default_writer;
    const last = (location.hash.match(/run=(\w+)/) || [])[1] || store.get("job_id");
    if (last) resume(last);
    loadPapers();
    route();
  }

  // ---------- drafts ----------
  const DRAFT_FIELDS = ["idea", "experimental_log", "template", "refine_rounds", "template_rules", "max_searches", "budget"];
  for (const id of DRAFT_FIELDS) {
    $(id).addEventListener("input", () => store.set("draft_" + id, $(id).value));
  }
  for (const id of ["use_literature", "use_web"]) {
    $(id).addEventListener("change", () => { store.set("draft_" + id, $(id).checked ? "1" : "0"); syncWebToggle(); });
  }
  function syncWebToggle() { $("use_web").disabled = !$("use_literature").checked; }

  $("toggleKey").addEventListener("click", () => {
    const show = $("api_key").type === "password";
    $("api_key").type = show ? "text" : "password";
    $("toggleKey").textContent = show ? "Hide" : "Show";
    $("toggleKey").setAttribute("aria-pressed", String(show));
  });
  $("remember").addEventListener("change", () => { if (!$("remember").checked) store.del("api_key"); });

  // Read text from any supported file via the local server (handles PDF and Word too)
  async function extractFile(file) {
    const fd = new FormData(); fd.append("file", file, file.name);
    const r = await fetch("/api/extract", { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `Could not read ${file.name}.`);
    return d;
  }

  // Load a file into a textarea (appends if the box already has text)
  let loadTarget = null;
  document.querySelectorAll("[data-load]").forEach(b => b.addEventListener("click", () => {
    loadTarget = b.dataset.load; $("textFile").value = ""; $("textFile").click();
  }));
  $("textFile").addEventListener("change", async () => {
    const f = $("textFile").files[0]; if (!f || !loadTarget) return;
    const box = $(loadTarget), btn = document.querySelector(`[data-load="${loadTarget}"]`);
    btn.disabled = true; btn.textContent = "Reading…"; showError("");
    try {
      const d = await extractFile(f);
      box.value = box.value.trim() ? `${box.value.trim()}\n\n---\n\n${d.text}` : d.text;
      store.set("draft_" + loadTarget, box.value);
      if (d.warning) showError(`${f.name}: ${d.warning}`);
      box.focus();
    } catch (err) { showError(netMessage(err)); }
    btn.disabled = false; btn.textContent = "Load file";
  });

  // ---------- documents ----------
  let docs = []; // {file, name, role, text, chars, status: reading|ready|error, message}
  const guessRole = (n) => /result|experiment|eval|ablation|benchmark|table|metric|\blog\b|data|\.(csv|tsv|json)$/i.test(n) ? "results" : "idea";
  function addDocs(list) {
    for (const file of list) {
      const doc = { file, name: file.name, role: guessRole(file.name), text: "", chars: 0, status: "reading", message: "Reading…" };
      docs.push(doc);
      extractFile(file).then(d => {
        doc.text = d.text; doc.chars = d.chars; doc.status = "ready";
        doc.message = `${d.chars.toLocaleString()} characters${d.warning ? " · " + d.warning : ""}`;
      }).catch(err => { doc.status = "error"; doc.message = err.message; })
        .finally(renderDocs);
    }
    renderDocs();
  }
  function renderDocs() {
    const ul = $("docList"); ul.innerHTML = "";
    docs.forEach((doc, i) => {
      const li = document.createElement("li"); li.className = "fig doc";
      li.innerHTML = `<div class="doc-main"><div class="name"></div><div class="status"></div></div>
        <div><label class="sr-only" for="role${i}">Use document ${i + 1} as</label>
          <select id="role${i}"><option value="idea">Idea</option><option value="results">Results</option><option value="both">Both</option></select></div>
        <button type="button" class="btn-sm" aria-label="Remove document ${i + 1}">Remove</button>`;
      li.querySelector(".name").textContent = doc.name;
      const st = li.querySelector(".status"); st.textContent = doc.message;
      if (doc.status === "error") st.classList.add("bad");
      const sel = li.querySelector("select"); sel.value = doc.role;
      sel.disabled = doc.status === "error";
      sel.addEventListener("change", () => { doc.role = sel.value; });
      li.querySelector("button").addEventListener("click", () => { docs.splice(docs.indexOf(doc), 1); renderDocs(); });
      ul.appendChild(li);
    });
  }
  $("docInput").addEventListener("change", (e) => {
    routeFiles([...e.target.files].map(f => ({ file: f, path: f.name }))); e.target.value = "";
  });

  // ---------- a whole folder: documents, figures and a captions file ----------
  const FIG_NAME = /([\w\-. ]+?\.(?:png|jpe?g|pdf))/i;
  function parseCaptions(text) {
    const caps = {}, lines = text.split(/\r?\n/);
    lines.forEach((line, i) => {
      const m = line.match(FIG_NAME); if (!m) return;
      let rest = line.slice(m.index + m[0].length).replace(/^[\s*_`:|\-\u2013\u2014]+/, "").replace(/[*_`]+$/, "").trim();
      if (!rest) rest = (lines.slice(i + 1, i + 3).find(l => l.trim() && !FIG_NAME.test(l)) || "").replace(/[*_`]/g, "").trim();
      if (rest) caps[m[1].trim().split("/").pop().toLowerCase()] = rest;
    });
    return caps;
  }
  function isFigure(path) {
    const low = path.toLowerCase();
    if (/\.(png|jpe?g)$/.test(low)) return true;
    if (!low.endsWith(".pdf")) return false;
    const parts = low.split("/");
    return parts[parts.length - 1].startsWith("fig") || parts.slice(0, -1).some(p => /^(figs?|figures?|imgs?|images?|plots|charts)$/.test(p));
  }
  // Sort any mix of files: documents are read, images become figures, a "caption" file fills in captions,
  // reviewer comments and unsupported files are skipped with a note. items: [{file, path}]
  async function routeFiles(items, { pdfAsFigure = false } = {}) {
    const all = items.filter(x => !/(^|\/)(\.|__MACOSX|__pycache__)/.test(x.path));
    const capFiles = all.filter(x => /caption/i.test(x.file.name) && /\.(md|txt|markdown)$/i.test(x.file.name));
    const figs = all.filter(x => !capFiles.includes(x) && (isFigure(x.path) || (pdfAsFigure && /\.pdf$/i.test(x.file.name))));
    const skipped = [], docs = [];
    for (const x of all) {
      if (capFiles.includes(x) || figs.includes(x)) continue;
      if (/review/i.test(x.file.name)) skipped.push(`${x.file.name} (reviewer comments: attach them in the editor chat)`);
      else if (/\.(md|markdown|txt|tex|pdf|docx|csv|tsv|json)$/i.test(x.file.name)) docs.push(x.file);
      else skipped.push(x.file.name);
    }
    if (docs.length) addDocs(docs);
    const before = figures.length;
    if (figs.length) addFiles(figs.map(x => x.file));
    let caps = {};
    for (const cf of capFiles) { try { caps = { ...caps, ...parseCaptions(await cf.file.text()) }; } catch {} }
    figures.slice(before).forEach(f => { const c = caps[f.file.name.toLowerCase()]; if (c && !f.caption) f.caption = c; });
    renderFigures();
    const captioned = figures.slice(before).filter(f => f.caption).length;
    const shown = skipped.slice(0, 5).join(", ") + (skipped.length > 5 ? ` and ${skipped.length - 5} more` : "");
    $("folderNote").textContent = `Added ${docs.length} document(s) and ${figs.length} figure(s)` +
      (figs.length ? ` (${captioned} with captions)` : "") + (skipped.length ? `. Skipped: ${shown}.` : ".");
  }

  // Files and folders dropped from Explorer/Finder (folders are read recursively)
  async function droppedItems(e) {
    const entries = [...(e.dataTransfer.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (!entries.length) return [...e.dataTransfer.files].map(f => ({ file: f, path: f.name }));
    const out = [];
    const readAll = (reader) => new Promise((resolve, reject) => {
      const acc = [];
      const next = () => reader.readEntries(batch => { if (!batch.length) resolve(acc); else { acc.push(...batch); next(); } }, reject);
      next();
    });
    const walk = async (entry, prefix) => {
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        out.push({ file, path: prefix + file.name });
      } else if (entry.isDirectory) {
        for (const child of await readAll(entry.createReader())) await walk(child, `${prefix}${entry.name}/`);
      }
    };
    for (const entry of entries) await walk(entry, "");
    return out;
  }

  $("folderBtn").addEventListener("click", () => { $("folderInput").value = ""; $("folderInput").click(); });
  $("folderInput").addEventListener("change", (e) => {
    routeFiles([...e.target.files].map(f => ({ file: f, path: f.webkitRelativePath || f.name })));
  });
  setupDrop($("docDrop"), async (e) => routeFiles(await droppedItems(e)));

  // ---------- custom LaTeX template ----------
  let tplFiles = []; // {file, path}
  function syncTemplate() { $("customTpl").hidden = $("template").value !== "__custom__"; }
  $("template").addEventListener("change", syncTemplate);
  $("tplFilesBtn").addEventListener("click", () => { $("tplFiles").value = ""; $("tplFiles").click(); });
  $("tplFolderBtn").addEventListener("click", () => { $("tplFolder").value = ""; $("tplFolder").click(); });
  $("tplFiles").addEventListener("change", (e) => setTpl([...e.target.files].map(f => ({ file: f, path: f.name }))));
  $("tplFolder").addEventListener("change", (e) => setTpl([...e.target.files].map(f => ({ file: f, path: f.webkitRelativePath || f.name }))));
  $("tplClearBtn").addEventListener("click", () => setTpl([]));
  function setTpl(list) {
    tplFiles = list.filter(x => !/(^|\/)(\.|__MACOSX)/.test(x.path));
    $("tplClearBtn").hidden = !tplFiles.length;
    if (!tplFiles.length) {
      $("tplSummary").textContent = "No template files chosen yet.";
      return;
    }
    const names = tplFiles.map(x => x.path);
    const shown = names.slice(0, 6).join(", ") + (names.length > 6 ? `, and ${names.length - 6} more` : "");
    $("tplSummary").textContent = `${names.length} file(s): ${shown}`;
  }

  // ---------- figures ----------
  function addFiles(list) {
    for (const file of list) {
      if (!/\.(png|jpe?g|pdf)$/i.test(file.name)) { showError(`"${file.name}" is not a PNG, JPG or PDF.`); continue; }
      const url = /\.pdf$/i.test(file.name) ? null : URL.createObjectURL(file);
      figures.push({ file, caption: "", url });
    }
    renderFigures();
  }
  function renderFigures() {
    const ul = $("figList"); ul.innerHTML = "";
    figures.forEach((f, i) => {
      const li = document.createElement("li"); li.className = "fig";
      const thumb = f.url ? `<img class="thumb" src="${f.url}" alt="">` : `<div class="thumb">PDF</div>`;
      li.innerHTML = `${thumb}
        <div><div class="name"></div>
          <label class="sr-only" for="cap${i}">Caption for figure ${i + 1}</label>
          <input type="text" id="cap${i}" placeholder="Caption, e.g. Accuracy vs. training steps"></div>
        <button type="button" class="btn-sm" aria-label="Remove figure ${i + 1}">Remove</button>`;
      li.querySelector(".name").textContent = f.file.name;
      const cap = li.querySelector("input"); cap.value = f.caption;
      cap.addEventListener("input", () => { f.caption = cap.value; });
      li.querySelector("button").addEventListener("click", () => {
        if (f.url) URL.revokeObjectURL(f.url);
        figures.splice(i, 1); renderFigures();
      });
      ul.appendChild(li);
    });
  }
  $("figInput").addEventListener("change", (e) => {
    routeFiles([...e.target.files].map(f => ({ file: f, path: f.name })), { pdfAsFigure: true }); e.target.value = "";
  });
  function setupDrop(el, onFiles) {
    ["dragenter", "dragover"].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.add("over"); }));
    ["dragleave", "drop"].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.remove("over"); }));
    el.addEventListener("drop", e => onFiles(e));
  }
  setupDrop($("drop"), async (e) => routeFiles(await droppedItems(e), { pdfAsFigure: true }));

  // ---------- submit ----------
  function showError(msg) { $("formError").textContent = msg || ""; }

  $("form").addEventListener("submit", async (e) => {
    e.preventDefault(); showError("");
    if (docs.some(d => d.status === "reading")) { showError("Wait until all documents have been read."); return; }
    const readyDocs = docs.filter(d => d.status === "ready");
    if (!$("idea").value.trim() && !$("experimental_log").value.trim() && !readyDocs.length) {
      showError("Add your idea, your results, or at least one document."); $("idea").focus(); return;
    }
    const custom = $("template").value === "__custom__";
    if (custom && !tplFiles.length) { showError("Choose your LaTeX template files, or pick a built-in template."); $("tplFilesBtn").focus(); return; }

    const key = $("api_key").value.trim();
    if ($("remember").checked && key) store.set("api_key", key);
    store.set("writer_model", $("writer_model").value.trim());
    store.set("search_model", $("search_model").value.trim());
    store.set("cutoff", $("cutoff").value);

    const fd = new FormData();
    fd.append("api_key", key);
    fd.append("writer_model", $("writer_model").value.trim());
    fd.append("search_model", $("search_model").value.trim());
    fd.append("template", $("template").value);
    fd.append("template_rules", custom ? $("template_rules").value : "");
    const materials = {
      idea: $("idea").value,
      experimental_log: $("experimental_log").value,
      documents: readyDocs.map(d => ({ name: d.name, role: d.role, text: d.text })),
    };
    fd.append("materials", new Blob([JSON.stringify(materials)], { type: "application/json" }), "materials.json");
    if (custom) tplFiles.forEach(x => fd.append("template_files", x.file, x.path));
    fd.append("cutoff", $("cutoff").value);
    fd.append("use_literature", $("use_literature").checked);
    fd.append("use_web", $("use_literature").checked && $("use_web").checked);
    fd.append("refine_rounds", $("refine_rounds").value);
    fd.append("max_searches", String(parseInt($("max_searches").value, 10) || 20));
    fd.append("budget", String(Math.max(0, parseFloat($("budget").value) || 0)));
    fd.append("s2_api_key", $("s2_api_key").value.trim());
    fd.append("openalex_api_key", $("openalex_api_key").value.trim());
    fd.append("captions", JSON.stringify(figures.map(f => f.caption)));
    figures.forEach(f => fd.append("figures", f.file, f.file.name));

    $("runBtn").disabled = true; $("runBtn").textContent = "Starting…";
    try {
      const r = await fetch("/api/jobs", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) {
        const detail = Array.isArray(d.detail) ? d.detail.map(x => x.msg).join("; ") : d.detail;
        throw new Error(detail || "Could not start the run.");
      }
      startJob(d.id, true);
    } catch (err) {
      showError(netMessage(err)); $("runBtn").disabled = false; $("runBtn").textContent = "Write my paper";
    }
  });

  // ---------- progress ----------
  function startJob(id, fresh) {
    jobId = id; logCount = 0; store.set("job_id", id);
    $("log").textContent = ""; $("files").innerHTML = ""; $("texPreview").hidden = true;
    $("previewBtn").hidden = true; $("emptyState").hidden = true; $("progress").hidden = false;
    $("cancelBtn").hidden = false; $("cancelBtn").disabled = false;
    $("runBtn").disabled = true; $("runBtn").textContent = "Writing…";
    startedAt = Date.now();
    clearInterval(elapsedTimer);
    elapsedTimer = setInterval(updateElapsed, 1000);
    if (fresh) $("h-progress").scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    poll();
  }
  async function resume(id) {
    const r = await fetch(`/api/jobs/${id}`);
    if (!r.ok) { store.del("job_id"); return; }
    startJob(id, false);
  }
  let finishedAt = null;
  function updateElapsed() {
    const s = Math.max(0, Math.floor(((finishedAt || Date.now()) - startedAt) / 1000));
    $("elapsed").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  async function poll() {
    clearTimeout(pollTimer);
    let d;
    try {
      const r = await fetch(`/api/jobs/${jobId}?since=${logCount}`);
      if (r.status === 404) { finish(); $("statusLine").textContent = "This run is no longer on the server."; return; }
      d = await r.json();
    } catch { pollTimer = setTimeout(poll, 4000); return; }
    render(d);
    if (d.status === "running") pollTimer = setTimeout(poll, 2000); else finish();
  }
  function render(d) {
    if (d.logs.length) {
      const el = $("log");
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      el.textContent += (el.textContent ? "\n" : "") + d.logs.join("\n");
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    logCount = d.log_count;
    if (d.started_at) startedAt = d.started_at * 1000;
    finishedAt = d.finished_at ? d.finished_at * 1000 : null;
    updateElapsed();
    $("cost").textContent = `$${d.cost.toFixed(2)}`;
    $("runDir").textContent = `Saved in ${d.run_dir}`;

    const idx = STEPS.findIndex(s => s[0] === d.step);
    $("steps").innerHTML = STEPS.map(([key, label], i) => {
      let cls = "", icon = "";
      if (d.status === "done" || i < idx) { cls = "done"; icon = ICON_CHECK; }
      else if (i === idx && d.status === "running") cls = "active";
      else if (i === idx && (d.status === "error" || d.status === "cancelled")) { cls = "failed"; icon = ICON_X; }
      const state = cls === "done" ? "done" : cls === "active" ? "in progress" : cls === "failed" ? "stopped" : "waiting";
      return `<li class="step ${cls}"><span class="dot">${icon}</span><span>${label}<span class="sr-only"> (${state})</span></span></li>`;
    }).join("");

    const line = $("statusLine");
    line.className = "status-line";
    if (d.status === "running") line.textContent = `Working on: ${STEPS[idx] ? STEPS[idx][1] : d.step}`;
    if (d.status === "done") { line.textContent = "Your paper is ready."; line.classList.add("ok"); }
    if (d.status === "error") { line.textContent = `Stopped with an error: ${d.error}`; line.classList.add("bad"); }
    if (d.status === "cancelled") { line.textContent = "Run cancelled."; line.classList.add("bad"); }

    const order = ["paper.pdf", "paper.zip", "main.tex", "references.bib", "outline.json", "reviews.json", "fact_check.json"];
    const files = d.files.slice().sort((a, b) => order.indexOf(a) - order.indexOf(b));
    $("files").innerHTML = files.map(f => `<a href="/api/jobs/${d.id}/files/${encodeURIComponent(f)}" download>${ICON_DL}${f}</a>`).join("");
    $("previewBtn").hidden = !d.files.includes("main.tex");
    $("openEditorBtn").hidden = !(d.status === "done" && d.project);
    $("openEditorBtn").dataset.project = d.project || "";
    if (d.status !== "running" && !render.listed) { render.listed = true; loadPapers(); }
  }
  $("openEditorBtn").addEventListener("click", () => { location.hash = "edit=" + $("openEditorBtn").dataset.project; });
  function finish() {
    clearInterval(elapsedTimer);
    $("cancelBtn").hidden = true;
    $("runBtn").disabled = false; $("runBtn").textContent = "Write my paper";
  }
  $("cancelBtn").addEventListener("click", async () => {
    $("cancelBtn").disabled = true;
    await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => {});
  });
  $("previewBtn").addEventListener("click", async () => {
    const pre = $("texPreview");
    const open = pre.hidden;
    if (open) pre.textContent = await fetch(`/api/jobs/${jobId}/files/main.tex`).then(r => r.text());
    pre.hidden = !open;
    $("previewBtn").setAttribute("aria-expanded", String(open));
    $("previewBtn").textContent = open ? "Hide main.tex" : "Preview main.tex";
  });

  // =====================================================================
  // Papers list and import
  // =====================================================================
  let defaultModel = "";
  let modelChoices = [];

  // Model boxes: a dropdown with the short list, plus "Other model..." that reveals a text box for any id
  function modelPicker(id) {
    const input = $(id);
    let sel = $(id + "Pick");
    if (!sel) {
      sel = document.createElement("select");
      sel.id = id + "Pick";
      input.parentNode.insertBefore(sel, input);
      const label = document.querySelector(`label[for="${id}"]`);
      if (label) label.htmlFor = sel.id;
      input.placeholder = "Any OpenRouter model id, or ollama/<model>";
      input.setAttribute("aria-label", "Other model id");
      sel.addEventListener("change", () => {
        if (sel.value === "__other__") { input.hidden = false; input.value = ""; input.focus(); }
        else { input.value = sel.value; input.hidden = true; }
        input.dispatchEvent(new Event("change"));
      });
    }
    sel.innerHTML = modelChoices.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)} · ${escapeHtml(m.label)}</option>`).join("")
      + `<option value="__other__">Other model…</option>`;
    syncModelPicker(id);
  }
  function syncModelPicker(id) {
    const input = $(id), sel = $(id + "Pick");
    if (!sel) return;
    const known = modelChoices.some(m => m.id === input.value);
    sel.value = known ? input.value : "__other__";
    input.hidden = known;
  }
  const OLD_DEFAULTS = ["google/gemini-3-flash-preview"];
  async function loadPapers() {
    const d = await fetch("/api/projects").then(r => r.json()).catch(() => ({ projects: [] }));
    const ul = $("papers"); ul.innerHTML = "";
    if (!d.projects.length) {
      ul.innerHTML = `<li class="empty">No papers yet.</li>`;
      return;
    }
    for (const p of d.projects) {
      const li = document.createElement("li");
      const b = document.createElement("button"); b.type = "button"; b.className = "p-open";
      b.innerHTML = `<span class="p-title"></span><span class="p-date"></span>`;
      b.querySelector(".p-title").textContent = p.title;
      b.querySelector(".p-date").textContent = new Date(p.updated * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
      b.title = p.title;
      b.setAttribute("aria-label", `Open ${p.title}`);
      b.addEventListener("click", () => { location.hash = "edit=" + p.id; });
      const del = document.createElement("button"); del.type = "button"; del.className = "p-del";
      del.title = "Delete this paper";
      del.setAttribute("aria-label", `Delete ${p.title}`);
      del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
      del.addEventListener("click", () => deletePaper(p, del));
      li.append(b, del); ul.appendChild(li);
    }
  }
  async function deletePaper(p, btn) {
    if (!confirm(`Delete "${p.title}"?

It's moved to the runs/_deleted folder, so you can still get it back from there.`)) return;
    btn.disabled = true; $("importError").textContent = "";
    try {
      const r = await fetch(`/api/projects/${p.id}`, { method: "DELETE" });
      const d = await r.json().catch(() => ({}));
      if (r.status === 405) { $("restartBanner").hidden = false; throw new Error("The app needs a restart before it can delete papers. Close the run.bat window, start it again, then reload this page."); }
      if (!r.ok) throw new Error(d.detail || "Couldn't delete this paper.");
      if ($("openEditorBtn").dataset.project === p.id) $("openEditorBtn").hidden = true;
      loadPapers();
    } catch (err) { $("importError").textContent = netMessage(err); btn.disabled = false; }
  }
  // "Failed to fetch" means the browser couldn't reach the local server at all
  function netMessage(err) {
    return err instanceof TypeError ? "Couldn't reach the app. Check that run.bat is still running (start it again if you closed it), then reload this page and try again." : err.message;
  }
  $("importBtn").addEventListener("click", () => { $("importFiles").value = ""; $("importFiles").click(); });
  $("importFiles").addEventListener("change", async (e) => {
    const files = [...e.target.files]; if (!files.length) return;
    const fd = new FormData(); files.forEach(f => fd.append("files", f, f.name));
    $("importBtn").disabled = true; $("importBtn").textContent = "Opening…"; $("importError").textContent = "";
    try {
      const r = await fetch("/api/projects/import", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Could not open that project.");
      location.hash = "edit=" + d.id;
    } catch (err) { $("importError").textContent = netMessage(err); }
    $("importBtn").disabled = false; $("importBtn").textContent = "Open a LaTeX project (.zip)";
  });

  // =====================================================================
  // Editor
  // =====================================================================
  const SVG = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
  const ICON = {
    chev: SVG('<polyline points="6 9 12 15 18 9"/>').replace("<svg", '<svg class="chev"'),
    folder: SVG('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
    tex: SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/>'),
    bib: SVG('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
    image: SVG('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>'),
    file: SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),
  };
  const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/";
  const ed = {
    id: null, data: null, open: null, openText: "", dirty: false, view: "pdf", pane: "chat",
    busy: false, compiling: false, selection: "", selFile: null, pdfStamp: 0, collapsed: new Set(), installPoll: null,
    attachments: [], pdf: { doc: null, scale: "fit", pages: [], token: 0, url: null, failed: false },
  };
  const SUGGESTIONS = [
    "Shorten the abstract to about 150 words",
    "Fix grammar, typos and awkward sentences in every file",
    "Add citations for the main claims in the introduction",
    "Make the supplementary material consistent with the main paper",
  ];
  const isSmall = () => matchMedia("(max-width: 899px)").matches;
  const isWide = () => matchMedia("(min-width: 1280px)").matches;
  const fileInfo = (p) => ed.data?.files.find(f => f.path === p);
  const rawUrl = (p) => `/api/projects/${ed.id}/raw?path=${encodeURIComponent(p)}&t=${Date.now()}`;
  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

  async function api(path, body) {
    const opts = body === undefined ? { method: "POST" } :
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(`/api/projects/${ed.id}${path}`, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Something went wrong.");
    return d;
  }
  async function apiGet(path) {
    const r = await fetch(`/api/projects/${ed.id}${path}`);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Something went wrong.");
    return d;
  }

  // ----- code editor: CodeMirror when it loaded, a plain textarea otherwise -----
  let cm = null, settingText = false;
  function initCodeEditor() {
    if (cm) return;
    if (!window.CodeMirror) { window.addEventListener("load", initCodeEditor, { once: true }); return; }
    const host = document.querySelector(".src-body");
    cm = window.CodeMirror(host, {
      lineNumbers: true, lineWrapping: true, matchBrackets: true, mode: "stex", indentUnit: 2, tabSize: 2,
      screenReaderLabel: "File contents",
      extraKeys: {
        "Ctrl-S": () => saveOrCompile(), "Cmd-S": () => saveOrCompile(),
        "Ctrl-Enter": () => saveOrCompile(), "Cmd-Enter": () => saveOrCompile(),
        "Ctrl-J": () => syncToPdf(), "Cmd-J": () => syncToPdf(),
      },
    });
    cm.on("change", () => { if (!settingText) onEdited(); });
    cm.on("cursorActivity", () => { const s = cm.getSelection().trim(); if (s) setSelection(s, ed.open); });
    // Take over from the plain textarea (the scripts may arrive after a file was opened)
    const ta = $("texEdit");
    settingText = true; cm.setValue(ta.value); cm.clearHistory(); settingText = false;
    if (ed.open) editor.mode(ed.open);
    cm.getWrapperElement().hidden = ta.hidden;
    ta.hidden = true;
  }
  const editor = {
    get: () => cm ? cm.getValue() : $("texEdit").value,
    set(text, keepScroll) {
      settingText = true;
      if (cm) {
        const sc = cm.getScrollInfo(), cur = cm.getCursor();
        cm.setValue(text); cm.clearHistory();
        if (keepScroll) { cm.scrollTo(sc.left, sc.top); cm.setCursor(cur); } else cm.scrollTo(0, 0);
      } else {
        const ta = $("texEdit"), top = ta.scrollTop; ta.value = text; ta.scrollTop = keepScroll ? top : 0;
      }
      settingText = false;
    },
    show(on) {
      if (cm) { cm.getWrapperElement().hidden = !on; if (on) setTimeout(() => cm.refresh(), 0); }
      else $("texEdit").hidden = !on;
    },
    mode(path) {
      if (!cm) return;
      const ext = (path.split(".").pop() || "").toLowerCase();
      cm.setOption("mode", ["tex", "sty", "cls", "bib", "bbl", "ltx", "dtx", "bst", "def", "cfg", "clo"].includes(ext) ? "stex" : null);
    },
    line: () => cm ? cm.getCursor().line + 1 : $("texEdit").value.slice(0, $("texEdit").selectionStart).split("\n").length,
    jump(line) {
      const n = Math.max(0, (line || 1) - 1);
      if (cm) {
        cm.setCursor({ line: n, ch: 0 });
        cm.scrollIntoView({ from: { line: Math.max(0, n - 3), ch: 0 }, to: { line: n + 8, ch: 0 } });
        const h = cm.addLineClass(n, "background", "cm-flash");
        setTimeout(() => cm.removeLineClass(h, "background", "cm-flash"), 1600);
        cm.focus();
      } else {
        const ta = $("texEdit"), lines = ta.value.split("\n");
        const pos = lines.slice(0, n).reduce((a, l) => a + l.length + 1, 0);
        ta.focus(); ta.setSelectionRange(pos, pos + (lines[n] || "").length);
        ta.scrollTop = Math.max(0, n - 5) * 20;
      }
    },
    refresh: () => { if (cm) cm.refresh(); },
  };

  function route() {
    const m = location.hash.match(/edit=([\w-]+)/);
    if (m) openEditor(m[1]); else closeEditor();
  }
  window.addEventListener("hashchange", route);

  async function openEditor(id) {
    document.body.classList.add("editor-mode");
    $("home").hidden = true; $("editor").hidden = false; $("backBtn").hidden = false;
    $("edTitle").hidden = false; $("brandName").hidden = true; $("edTools").hidden = false;
    initCodeEditor();
    if (ed.id !== id) {
      Object.assign(ed, { id, data: null, open: null, openText: "", dirty: false, selection: "", selFile: null, pdfStamp: 0, attachments: [] });
      ed.pdf = { doc: null, scale: "fit", pages: [], token: 0, url: null, failed: false };
      ed.collapsed.clear();
      $("panePdf").innerHTML = ""; editor.set(""); $("msgs").innerHTML = ""; $("tree").innerHTML = "";
      renderAttachments();
    }
    $("edModel").value = store.get("editor_model") || store.get("writer_model") || defaultModel;
    syncModelPicker("edModel");
    $("zipLink").href = `/api/projects/${id}/zip`;
    $("pdfLink").hidden = true;
    setPane(isSmall() ? "chat" : "source");
    setView("pdf");
    try {
      const d = await apiGet("");
      if (!Array.isArray(d.files)) {
        $("restartBanner").hidden = false;
        throw new Error("The app's server is an older version. Close the run.bat window, start it again, then reload this page.");
      }
      ed.data = d;
      renderEditor();
      await openFile(fileInfo(ed.open) ? ed.open : d.main, { quiet: true });
      const stale = !d.compile || d.compile.version !== d.version || d.compile.main !== d.main || !d.pdf;
      if (d.latex.engine && stale && !d.busy) compile();
      if (d.busy) { setBusy(true); renderMessages(); pollLive(); }
    } catch (err) {
      $("panePdf").innerHTML = ""; $("panePdf").appendChild(emptyState("Couldn't open this paper", err.message));
    }
  }
  function closeEditor() {
    if ($("editor").hidden) return;
    document.body.classList.remove("editor-mode");
    $("home").hidden = false; $("editor").hidden = true; $("backBtn").hidden = true;
    $("edTitle").hidden = true; $("brandName").hidden = false; $("edTools").hidden = true;
    document.title = "PaperScriptor";
    loadPapers();
  }
  $("backBtn").addEventListener("click", async () => {
    if (ed.dirty && !(await saveFile(false))) return;
    ed.id = null;
    history.pushState("", "", location.pathname); route();
  });
  window.addEventListener("beforeunload", (e) => { if (ed.dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ----- panes -----
  function setPane(p) {   // phones: one pane at a time
    ed.pane = p; $("edGrid").dataset.pane = p;
    document.querySelectorAll(".ed-tabs [data-pane]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.pane === p)));
    if (p === "source" || p === "pdf") setView(p);
    if (p === "chat") setTimeout(scrollChat, 0);
    if (p === "source") editor.refresh();
    if (p === "pdf") setTimeout(refitPdf, 0);
  }
  function setView(v) {   // medium screens: source or PDF in the middle
    ed.view = v; $("edGrid").dataset.view = v;
    document.querySelectorAll("button[data-view]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === v)));
    if (v === "source") setTimeout(() => editor.refresh(), 0); else setTimeout(refitPdf, 0);
  }
  document.querySelectorAll(".ed-tabs [data-pane]").forEach(b => b.addEventListener("click", () => setPane(b.dataset.pane)));
  document.querySelectorAll("button[data-view]").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
  function showSource() { if (isSmall()) setPane("source"); else if (!isWide()) setView("source"); }
  function showPdf() { if (isSmall()) setPane("pdf"); else if (!isWide()) setView("pdf"); }

  function renderEditor() {
    const d = ed.data; if (!d) return;
    $("edTitle").textContent = d.title;
    document.title = `${d.title} · PaperScriptor`;
    $("edCost").textContent = `$${d.cost.toFixed(d.cost < 0.1 ? 3 : 2)}`;
    $("versionSel").innerHTML = d.versions.slice().reverse().map(v =>
      `<option value="${v.n}">${v.n === d.version ? "Current: " : ""}v${v.n} · ${escapeHtml(v.note)}</option>`).join("");
    $("versionSel").value = String(d.version);
    const mc = d.main_candidates || [];
    const cands = mc.includes(d.main) ? mc : [d.main, ...mc];
    $("mainSel").innerHTML = cands.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
    $("mainSel").value = d.main;
    renderTree();
    renderSourceHead();
    if (!ed.busy) renderMessages();
    renderPdf();
    renderBanner();
    $("compileBtn").disabled = ed.compiling;
    $("pdfLink").hidden = !d.pdf;
    $("pdfLink").href = `/api/projects/${ed.id}/pdf?download=1&t=${d.pdf_mtime}`;
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
  function scrollChat() { const m = $("msgs"); m.scrollTop = m.scrollHeight; }

  // ----- file tree -----
  function renderTree() {
    const root = { dirs: {}, files: [] };
    for (const f of ed.data.files) {
      const parts = f.path.split("/"); let node = root;
      parts.slice(0, -1).forEach((part, i) => {
        const key = parts.slice(0, i + 1).join("/");
        node = node.dirs[part] ||= { key, dirs: {}, files: [] };
      });
      node.files.push(f);
    }
    const ul = $("tree"); ul.innerHTML = "";
    const draw = (node, depth, parent) => {
      for (const name of Object.keys(node.dirs).sort((a, b) => a.localeCompare(b))) {
        const dir = node.dirs[name], open = !ed.collapsed.has(dir.key);
        const li = document.createElement("li");
        const b = document.createElement("button"); b.type = "button";
        b.style.paddingLeft = `${8 + depth * 14}px`;
        b.setAttribute("aria-expanded", String(open));
        b.innerHTML = `${ICON.chev}${ICON.folder}<span class="nm"></span>`;
        b.querySelector(".nm").textContent = name;
        b.addEventListener("click", () => { open ? ed.collapsed.add(dir.key) : ed.collapsed.delete(dir.key); renderTree(); });
        li.appendChild(b);
        if (open) { const sub = document.createElement("ul"); draw(dir, depth + 1, sub); li.appendChild(sub); }
        parent.appendChild(li);
      }
      for (const f of node.files.sort((a, b) => a.path.localeCompare(b.path))) {
        const li = document.createElement("li");
        const b = document.createElement("button"); b.type = "button";
        b.style.paddingLeft = `${8 + depth * 14 + 15}px`;
        if (f.path === ed.open) b.setAttribute("aria-current", "true");
        const icon = f.kind === "tex" || f.kind === "text" ? ICON.tex : f.kind === "bib" ? ICON.bib : f.kind === "image" ? ICON.image : ICON.file;
        b.innerHTML = `${icon}<span class="nm"></span>${f.path === ed.data.main ? '<span class="badge">Main</span>' : ""}`;
        b.querySelector(".nm").textContent = (f.path === ed.open && ed.dirty ? "● " : "") + f.path.split("/").pop();
        b.title = f.path;
        b.addEventListener("click", () => openFile(f.path));
        li.appendChild(b); parent.appendChild(li);
      }
    };
    draw(root, 0, ul);
  }

  // ----- source pane -----
  function renderSourceHead() {
    const d = ed.data, p = ed.open, info = fileInfo(p);
    $("srcTitle").innerHTML = p ? `${ed.dirty ? '<span class="dirty">● </span>' : ""}<span></span>` : "No file open";
    if (p) $("srcTitle").lastChild.textContent = p;
    $("setMainBtn").hidden = !p || p === d.main || !p.toLowerCase().endsWith(".tex");
    $("saveBtn").hidden = !ed.dirty;
    $("deleteBtn").disabled = !p || p === d.main;
    $("deleteBtn").title = p === d.main ? "The main file can't be deleted" : "Delete this file";
    $("renameBtn").disabled = !p;
    $("downloadBtn").hidden = !info;
    if (info) $("downloadBtn").href = `/api/projects/${ed.id}/raw?path=${encodeURIComponent(p)}&download=1`;
    $("syncBtn").hidden = !(info && info.kind === "tex" && d.pdf);
  }

  async function openFile(path, { quiet = false, line = null } = {}) {
    if (!path) return;
    if (ed.dirty && ed.open !== path && !(await saveFile(false))) return;
    const info = fileInfo(path);
    const same = ed.open === path;
    ed.open = path;
    if (!same) ed.dirty = false;
    if (ed.selFile && ed.selFile !== path) setSelection("");
    const viewer = $("viewer");
    if (info && info.editable) {
      if (!same || !ed.openText) {
        try {
          const d = await apiGet(`/file?path=${encodeURIComponent(path)}`);
          if (ed.open !== path) return;
          ed.openText = d.text; editor.mode(path); editor.set(d.text, false);
        } catch (err) { editor.show(false); viewer.hidden = false; viewer.textContent = err.message; return; }
      }
      editor.show(true); viewer.hidden = true;
    } else {
      editor.show(false); viewer.hidden = false; viewer.innerHTML = "";
      if (info?.kind === "image") {
        const img = document.createElement("img"); img.src = rawUrl(path); img.alt = path; viewer.appendChild(img);
      } else if (info?.kind === "pdf") {
        const f = document.createElement("iframe"); f.src = rawUrl(path) + "#view=FitH"; f.title = path; viewer.appendChild(f);
      } else {
        viewer.textContent = info ? "This file can't be shown here, but it stays in the project and is used when compiling." : "File not found.";
      }
    }
    renderTree(); renderSourceHead();
    if (!quiet) showSource();
    if (line) setTimeout(() => editor.jump(line), 30);
  }

  async function refreshOpen() {
    if (!ed.open || !fileInfo(ed.open)) { ed.open = null; ed.dirty = false; return openFile(ed.data.main, { quiet: true }); }
    if (ed.dirty || !fileInfo(ed.open).editable) return;
    try {
      const d = await apiGet(`/file?path=${encodeURIComponent(ed.open)}`);
      if (d.text !== ed.openText && !ed.dirty) { ed.openText = d.text; editor.set(d.text, true); }
    } catch {}
  }

  function onEdited() {
    const was = ed.dirty;
    ed.dirty = editor.get() !== ed.openText;
    if (was !== ed.dirty) { renderSourceHead(); renderTree(); }
  }
  $("texEdit").addEventListener("input", onEdited);
  $("texEdit").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "s" || e.key === "Enter")) { e.preventDefault(); saveOrCompile(); }
  });
  ["select", "mouseup", "keyup"].forEach(ev => $("texEdit").addEventListener(ev, () => {
    const ta = $("texEdit"); const s = ta.value.slice(ta.selectionStart, ta.selectionEnd).trim();
    if (s) setSelection(s, ed.open);
  }));
  function saveOrCompile() { if (ed.dirty) saveFile(true); else compile(); }
  function setSelection(s, file = null) {
    ed.selection = s; ed.selFile = s ? file : null;
    $("selChip").hidden = !s;
    $("selText").textContent = s ? `Selected in ${file}: ${s.replace(/\s+/g, " ").slice(0, 100)}` : "";
  }
  $("selClear").addEventListener("click", () => setSelection(""));
  $("saveBtn").addEventListener("click", () => saveFile(true));

  async function saveFile(compileAfter) {
    if (!ed.dirty) return true;
    const path = ed.open, text = editor.get();
    $("saveBtn").disabled = true;
    try {
      ed.data = await api("/file", { path, text });
      if (ed.open === path) { ed.openText = text; ed.dirty = editor.get() !== text; }
      renderEditor();
      if (compileAfter && ed.data.latex.engine) compile();
      return true;
    } catch (err) { alert(err.message); return false; }
    finally { $("saveBtn").disabled = false; }
  }

  $("setMainBtn").addEventListener("click", () => setMain(ed.open));
  $("mainSel").addEventListener("change", () => setMain($("mainSel").value));
  async function setMain(path) {
    if (!path || path === ed.data.main) return;
    if (ed.dirty && !(await saveFile(false))) return;
    try {
      ed.data = await api("/main", { path });
      ed.pdfStamp = 0; ed.pdf.url = null;
      renderEditor();
      if (ed.data.latex.engine) { showPdf(); compile(); }
    } catch (err) { alert(err.message); $("mainSel").value = ed.data.main; }
  }

  $("newFileBtn").addEventListener("click", async () => {
    const base = ed.open && ed.open.includes("/") ? ed.open.slice(0, ed.open.lastIndexOf("/") + 1) : "";
    const name = (prompt("Name of the new file, for example supplementary.tex or sections/results.tex", base) || "").trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if (!name || name.endsWith("/")) return;
    if (ed.dirty && !(await saveFile(false))) return;
    try {
      ed.data = await api("/new", { path: name });
      renderEditor();
      if (fileInfo(name)) openFile(name);
    } catch (err) { alert(err.message); }
  });
  $("renameBtn").addEventListener("click", async () => {
    const p = ed.open; if (!p) return;
    const name = (prompt("New name or path for this file", p) || "").trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if (!name || name === p) return;
    if (ed.dirty && !(await saveFile(false))) return;
    try {
      ed.data = await api("/rename", { path: p, new_path: name });
      ed.open = null; ed.openText = "";
      renderEditor();
      await openFile(fileInfo(name) ? name : ed.data.main, { quiet: true });
    } catch (err) { alert(err.message); }
  });
  async function uploadFiles(files, folder = "") {
    if (!files.length) return;
    const fd = new FormData(); files.forEach(f => fd.append("files", f, f.name)); fd.append("folder", folder);
    $("uploadBtn").disabled = true;
    try {
      const r = await fetch(`/api/projects/${ed.id}/upload`, { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Upload failed.");
      ed.data = d; renderEditor();
    } catch (err) { alert(netMessage(err)); }
    $("uploadBtn").disabled = false;
  }
  $("uploadBtn").addEventListener("click", () => { $("uploadInput").value = ""; $("uploadInput").click(); });
  $("uploadInput").addEventListener("change", (e) => {
    const files = [...e.target.files]; if (!files.length) return;
    const dir = ed.open && ed.open.includes("/") ? ed.open.slice(0, ed.open.lastIndexOf("/")) : "";
    const folder = dir && confirm(`Put the files in the folder "${dir}"?\n\nPress Cancel to put them in the top folder.`) ? dir : "";
    uploadFiles(files, folder);
  });
  (() => {   // drag and drop onto the Files pane
    const pane = document.querySelector(".ed-grid .files");
    ["dragenter", "dragover"].forEach(ev => pane.addEventListener(ev, (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); pane.classList.add("drag-over"); } }));
    ["dragleave", "drop"].forEach(ev => pane.addEventListener(ev, (e) => { if (ev === "dragleave" && pane.contains(e.relatedTarget)) return; pane.classList.remove("drag-over"); }));
    pane.addEventListener("drop", (e) => { e.preventDefault(); uploadFiles([...e.dataTransfer.files]); });
  })();
  $("deleteBtn").addEventListener("click", async () => {
    const p = ed.open; if (!p || p === ed.data.main) return;
    if (!confirm(`Delete ${p}?` + (fileInfo(p)?.editable ? " You can bring it back with the version list." : ""))) return;
    try {
      ed.data = await api("/delete", { path: p });
      ed.dirty = false; ed.open = null; ed.openText = "";
      renderEditor();
      await openFile(ed.data.main, { quiet: true });
    } catch (err) { alert(err.message); }
  });

  // ----- versions -----
  $("versionSel").addEventListener("change", () => {
    const n = Number($("versionSel").value);
    if (n !== ed.data.version) restoreVersion(n);
  });
  async function restoreVersion(n) {
    if (ed.busy) return;
    if (ed.dirty && !confirm("Discard your unsaved changes?")) { $("versionSel").value = String(ed.data.version); return; }
    try {
      ed.data = await api("/restore", { version: n });
      ed.dirty = false;
      renderEditor();
      await refreshOpen();
      if (ed.data.latex.engine) compile();
    } catch (err) { alert(err.message); }
  }

  // ----- messages -----
  function renderMessages() {
    const box = $("msgs"); box.innerHTML = "";
    const msgs = ed.data.messages;
    if (!msgs.length && !ed.busy) {
      const div = document.createElement("div"); div.className = "chat-empty";
      div.innerHTML = `<h2>What should change?</h2>
        <p>Ask in plain words. The model can edit any text file in the project, several at once, and can add real,
        checked references. Select text in the editor to point at a passage, or attach notes and results with the clip.
        ${ed.data.has_inputs ? "" : " This project has no notes or results attached, so the model won't add new numbers."}</p>
        <div class="suggest"></div>`;
      SUGGESTIONS.forEach(s => {
        const b = document.createElement("button"); b.type = "button"; b.textContent = s;
        b.addEventListener("click", () => { $("prompt").value = s; autoGrow(); $("prompt").focus(); });
        div.querySelector(".suggest").appendChild(b);
      });
      box.appendChild(div);
    }
    const lastEdit = [...msgs].reverse().find(m => m.role === "assistant" && m.version);
    for (const m of msgs) box.appendChild(messageEl(m, m === lastEdit && m.version === ed.data.version));
    if (ed.busy) {
      const t = document.createElement("div"); t.className = "msg assistant"; t.id = "liveMsg";
      t.innerHTML = `<div id="liveText"></div><div class="live-stage"><span class="spinner"></span><span id="liveStage">Thinking…</span></div>`;
      box.appendChild(t);
    }
    scrollChat();
  }

  function messageEl(m, isLatest) {
    const div = document.createElement("div");
    if (m.role === "event") { div.className = "event"; div.textContent = m.text; return div; }
    div.className = `msg ${m.role}${m.error ? " error" : ""}`;
    if (m.attachments?.length) {
      const a = document.createElement("div"); a.className = "atts";
      m.attachments.forEach(n => { const s = document.createElement("span"); s.textContent = n; a.appendChild(s); });
      div.appendChild(a);
    }
    if (m.selection) {
      const q = document.createElement("div"); q.className = "quote";
      q.textContent = (m.selection_file ? `${m.selection_file}: ` : "") + m.selection; div.appendChild(q);
    }
    div.appendChild(document.createTextNode(m.text));
    if (m.role === "assistant" && m.version) {
      const foot = document.createElement("div"); foot.className = "msg-foot";
      const files = m.diff.files || [];
      foot.innerHTML = `<span><span class="add">+${m.diff.added}</span> <span class="del">−${m.diff.removed}</span> lines${files.length > 1 ? ` in ${files.length} files` : ""} · v${m.version}</span>`;
      const show = document.createElement("button"); show.type = "button"; show.textContent = "Show changes";
      show.setAttribute("aria-expanded", "false");
      let pre = null;
      show.addEventListener("click", () => {
        if (!pre) { pre = diffEl(m.diff.text); div.appendChild(pre); } else pre.hidden = !pre.hidden;
        const open = !pre.hidden; show.textContent = open ? "Hide changes" : "Show changes";
        show.setAttribute("aria-expanded", String(open));
      });
      foot.appendChild(show);
      if (isLatest && m.prev_version) {
        const undo = document.createElement("button"); undo.type = "button"; undo.textContent = "Undo";
        undo.addEventListener("click", () => restoreVersion(m.prev_version));
        foot.appendChild(undo);
      }
      if (files.length) {
        const fl = document.createElement("div"); fl.className = "msg-files";
        files.forEach((p, i) => {
          if (i) fl.appendChild(document.createTextNode(", "));
          const a = document.createElement("a"); a.href = "#"; a.textContent = p;
          a.addEventListener("click", (e) => { e.preventDefault(); if (fileInfo(p)) openFile(p); });
          fl.appendChild(a);
        });
        foot.appendChild(fl);
      }
      div.appendChild(foot);
    } else if (m.role === "assistant" && m.cost && !m.error) {
      const foot = document.createElement("div"); foot.className = "msg-foot"; foot.textContent = "No files changed.";
      div.appendChild(foot);
    }
    return div;
  }

  function diffEl(text) {
    const pre = document.createElement("pre"); pre.className = "diff";
    for (const line of text.split("\n")) {
      const s = document.createElement("span");
      s.className = line.startsWith("### ") ? "d-file" : line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del" : line.startsWith("@@") ? "d-hunk" : "";
      s.textContent = line.startsWith("### ") ? line.slice(4) : line.startsWith("@@") ? "···" : line || " ";
      pre.appendChild(s);
    }
    return pre;
  }

  // ----- PDF: PDF.js viewer that keeps your place, with the browser viewer as fallback -----
  function emptyState(title, text, buttons = []) {
    const div = document.createElement("div"); div.className = "pv-empty";
    const h = document.createElement("h2"); h.textContent = title; div.appendChild(h);
    if (text) { const p = document.createElement("p"); p.textContent = text; div.appendChild(p); }
    if (buttons.length) {
      const row = document.createElement("div"); row.className = "actions"; row.style.justifyContent = "center";
      buttons.forEach(b => row.appendChild(b)); div.appendChild(row);
    }
    return div;
  }
  function makeBtn(label, onClick, solid) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (solid) b.className = "btn-solid"; b.addEventListener("click", onClick); return b;
  }
  function statusPill(text, spin = true) {
    const s = document.createElement("div"); s.className = "pill-status"; s.setAttribute("role", "status");
    s.innerHTML = `${spin ? '<span class="spinner"></span>' : ""}<span></span>`; s.lastChild.textContent = text; return s;
  }
  function withSpinner(el) { el.querySelector(".pv-empty").prepend(Object.assign(document.createElement("span"), { className: "spinner" })); }

  let pdfjsLib = null;
  async function loadPdfjs() {
    if (pdfjsLib) return pdfjsLib;
    const mod = await import(PDFJS + "pdf.min.mjs");
    mod.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.mjs";
    return (pdfjsLib = mod);
  }

  function renderSetupBanner() {
    const d = ed.data, lt = d.latex, b = $("setupBanner");
    b.hidden = !!lt.engine || !d.pdf;   // with no PDF at all, the big empty state offers setup instead
    if (b.hidden) return;
    b.innerHTML = "";
    const p = document.createElement("p");
    if (lt.installing) {
      b.appendChild(Object.assign(document.createElement("span"), { className: "spinner" }));
      p.textContent = "Setting up PDF preview: downloading Tectonic (about 21 MB). This happens only once.";
      b.appendChild(p);
      return;
    }
    p.textContent = (lt.error ? lt.error + " " : "") +
      "This PDF came with your project, so it won't change when you edit. To rebuild it, set up a LaTeX engine once" +
      (lt.can_install ? " (free, about 21 MB)." : ": install TeX Live or Tectonic, then reload this page.");
    b.appendChild(p);
    if (lt.can_install) b.appendChild(makeBtn(lt.error ? "Try again" : "Set up PDF preview", setupLatex, true));
  }

  function renderPdf() {
    const d = ed.data, pane = $("panePdf"), lt = d.latex;
    renderSetupBanner();
    pane.querySelectorAll(".pill-status").forEach(x => x.remove());
    $("zoomBar").hidden = !d.pdf || ed.pdf.failed;
    if (d.pdf) {
      const url = `/api/projects/${ed.id}/pdf?t=${d.pdf_mtime}`;
      if (ed.pdfStamp !== d.pdf_mtime || !pane.querySelector(".pdfv, iframe")) { ed.pdfStamp = d.pdf_mtime; showPdfDoc(url); }
      if (ed.compiling) pane.appendChild(statusPill(`Compiling ${d.main}…`));
      return;
    }
    ed.pdf.doc = null; $("pageInd").textContent = "";
    pane.innerHTML = "";
    if (ed.compiling) {
      pane.appendChild(emptyState(`Compiling ${d.main}…`, "The first time can take a few minutes while LaTeX packages download.")); withSpinner(pane);
    } else if (lt.installing) {
      pane.appendChild(emptyState("Setting up PDF preview…", "Downloading Tectonic (about 21 MB). This happens only once.")); withSpinner(pane);
    } else if (!lt.engine) {
      pane.appendChild(emptyState("PDF preview needs LaTeX",
        (lt.error ? lt.error + " " : "") + (lt.can_install
          ? "Press the button to download Tectonic, a free LaTeX engine (about 21 MB, from GitHub). The first compile then downloads the LaTeX packages it needs. If you already use MiKTeX or TeX Live, it's used instead."
          : "Install TeX Live or Tectonic, then reload this page."),
        lt.can_install ? [makeBtn("Set up PDF preview", setupLatex, true)] : []));
    } else {
      const c = d.compile;
      pane.appendChild(emptyState(c && !c.pdf ? "The PDF couldn't be built" : "No PDF yet",
        c && !c.pdf ? "See the errors above. You can ask the chat to fix them." : `Main file: ${d.main}`, [makeBtn("Compile", compile, true)]));
    }
  }

  function currentPdfPos() {
    const v = $("panePdf").querySelector(".pdfv"); if (!v || !ed.pdf.pages.length) return null;
    const top = v.scrollTop;
    for (const p of ed.pdf.pages) {
      if (p.div.offsetTop + p.div.offsetHeight > top) return { page: p.num, frac: Math.max(0, (top - p.div.offsetTop) / p.div.offsetHeight) };
    }
    return null;
  }
  function pdfScale(baseWidth, container) {
    if (ed.pdf.scale === "fit") return Math.max(0.3, (container.clientWidth - 32) / baseWidth);
    return ed.pdf.scale;
  }

  async function showPdfDoc(url, keepPos = true) {
    const pane = $("panePdf");
    if (ed.pdf.failed) { pane.innerHTML = ""; const f = document.createElement("iframe"); f.title = "PDF preview"; f.src = url + "#view=FitH"; pane.appendChild(f); return; }
    const token = ++ed.pdf.token;
    const pos = keepPos ? currentPdfPos() : null;
    let lib, doc;
    try {
      lib = await loadPdfjs();
      doc = await lib.getDocument({ url, isEvalSupported: false }).promise;
    } catch (err) {
      console.warn("PDF.js unavailable, using the browser viewer", err);
      ed.pdf.failed = true; $("zoomBar").hidden = true;
      return showPdfDoc(url, false);
    }
    if (token !== ed.pdf.token) return;
    const first = await doc.getPage(1);
    const base = first.getViewport({ scale: 1 });
    let viewer = pane.querySelector(".pdfv");
    const fresh = !viewer;
    const holder = document.createElement("div");
    const scale = pdfScale(base.width, viewer || pane);
    const pages = [];
    for (let n = 1; n <= doc.numPages; n++) {
      const div = document.createElement("div"); div.className = "pdfv-page"; div.dataset.page = n;
      div.style.width = `${base.width * scale}px`; div.style.height = `${base.height * scale}px`;
      holder.appendChild(div);
      pages.push({ num: n, div, rendered: false, scale });
    }
    ed.pdf.doc = doc; ed.pdf.url = url; ed.pdf.pages = pages; ed.pdf.base = base;
    // Render the pages around the old position before swapping, so the view doesn't jump or flash
    const target = pos ? Math.min(pos.page, doc.numPages) : 1;
    await Promise.all(pages.filter(p => Math.abs(p.num - target) <= 1).map(p => renderPage(p, token)));
    if (token !== ed.pdf.token) return;
    if (fresh) {
      pane.innerHTML = "";
      viewer = document.createElement("div"); viewer.className = "pdfv"; viewer.tabIndex = 0;
      viewer.setAttribute("aria-label", "PDF preview. Double-click to jump to the source.");
      pane.appendChild(viewer);
      viewer.addEventListener("scroll", onPdfScroll, { passive: true });
      viewer.addEventListener("dblclick", onPdfDblClick);
    }
    viewer.replaceChildren(...holder.childNodes);
    if (pos) { const p = pages[target - 1]; viewer.scrollTop = p.div.offsetTop + pos.frac * p.div.offsetHeight; }
    else viewer.scrollTop = 0;
    if (ed.compiling) pane.appendChild(statusPill(`Compiling ${ed.data.main}…`));
    observePages(viewer);
    updateZoomLabel(); onPdfScroll();
  }

  let pageObserver = null;
  function observePages(viewer) {
    pageObserver?.disconnect();
    const token = ed.pdf.token;
    pageObserver = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const p = ed.pdf.pages[Number(e.target.dataset.page) - 1];
        if (p && !p.rendered) renderPage(p, token);
      }
    }, { root: viewer, rootMargin: "600px 0px" });
    ed.pdf.pages.forEach(p => pageObserver.observe(p.div));
  }

  async function renderPage(p, token) {
    if (p.rendered || !ed.pdf.doc) return;
    p.rendered = true;
    try {
      const page = await ed.pdf.doc.getPage(p.num);
      if (token !== ed.pdf.token) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      const vp = page.getViewport({ scale: p.scale * dpr });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width); canvas.height = Math.floor(vp.height);
      canvas.setAttribute("aria-label", `Page ${p.num}`);
      await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
      if (token !== ed.pdf.token) return;
      p.div.querySelector("canvas")?.remove();
      p.div.prepend(canvas);
    } catch (err) { p.rendered = false; }
  }

  function onPdfScroll() {
    const pos = currentPdfPos();
    $("pageInd").textContent = pos && ed.pdf.doc ? `${pos.page} / ${ed.pdf.doc.numPages}` : "";
  }
  function updateZoomLabel() {
    const p = ed.pdf.pages[0];
    $("zoomFit").textContent = ed.pdf.scale === "fit" ? "Fit" : `${Math.round((p ? p.scale : 1) * 100)}%`;
  }
  function setZoom(next) {
    if (!ed.pdf.doc) return;
    ed.pdf.scale = next;
    showPdfDoc(ed.pdf.url, true);
  }
  $("zoomFit").addEventListener("click", () => setZoom("fit"));
  $("zoomIn").addEventListener("click", () => { const s = ed.pdf.pages[0]?.scale || 1; setZoom(Math.min(4, +(s * 1.2).toFixed(2))); });
  $("zoomOut").addEventListener("click", () => { const s = ed.pdf.pages[0]?.scale || 1; setZoom(Math.max(0.3, +(s / 1.2).toFixed(2))); });
  let refitTimer = null;
  function refitPdf() {
    if (!ed.pdf.doc || ed.pdf.scale !== "fit") return;
    const v = $("panePdf").querySelector(".pdfv"); if (!v || !v.clientWidth) return;
    const want = pdfScale(ed.pdf.base.width, v);
    if (Math.abs(want - (ed.pdf.pages[0]?.scale || 0)) > 0.01) showPdfDoc(ed.pdf.url, true);
  }
  new ResizeObserver(() => { clearTimeout(refitTimer); refitTimer = setTimeout(refitPdf, 250); }).observe($("panePdf"));

  async function onPdfDblClick(e) {
    const pageDiv = e.target.closest(".pdfv-page"); if (!pageDiv) return;
    const p = ed.pdf.pages[Number(pageDiv.dataset.page) - 1];
    const rect = pageDiv.getBoundingClientRect();
    const x = (e.clientX - rect.left) / p.scale, y = (e.clientY - rect.top) / p.scale;
    try {
      const hit = await apiGet(`/synctex/inverse?page=${p.num}&x=${x.toFixed(1)}&y=${y.toFixed(1)}`);
      await openFile(hit.path, { line: hit.line });
    } catch (err) { flashPill(err.message); }
  }
  async function syncToPdf() {
    if (!ed.open || !ed.pdf.doc) return;
    try {
      const hit = await apiGet(`/synctex/forward?path=${encodeURIComponent(ed.open)}&line=${editor.line()}`);
      showPdf();
      const p = ed.pdf.pages[hit.page - 1], v = $("panePdf").querySelector(".pdfv");
      if (!p || !v) return;
      v.scrollTo({ top: Math.max(0, p.div.offsetTop + hit.y * p.scale - 120), behavior: smooth() });
      const mark = document.createElement("div"); mark.className = "pdfv-mark"; mark.style.top = `${hit.y * p.scale}px`;
      p.div.appendChild(mark); setTimeout(() => mark.remove(), 2000);
    } catch (err) { flashPill(err.message); }
  }
  $("syncBtn").addEventListener("click", syncToPdf);
  function flashPill(text) {
    const pill = statusPill(text, false); $("panePdf").appendChild(pill); setTimeout(() => pill.remove(), 3500);
  }

  function renderMissing() {
    const c = ed.data.compile, b = $("missingBanner");
    const list = c && c.main === ed.data.main && !ed.compiling ? (c.missing || []) : [];
    b.hidden = !list.length; if (!list.length) return;
    b.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = `${list.length} figure${list.length > 1 ? "s are" : " is"} missing from the project, so grey placeholders are shown. ` +
      "Upload the image files (or drag them onto Files) and the PDF updates.";
    b.appendChild(p);
    b.appendChild(makeBtn("Upload figures", () => { $("figUploadInput").value = ""; $("figUploadInput").click(); }, true));
    const ul = document.createElement("ul"); ul.className = "fig-list";
    list.forEach(m => {
      const li = document.createElement("li");
      const btn = document.createElement("button"); btn.type = "button";
      btn.textContent = `${m.name} (${m.file}, line ${m.line})`;
      btn.addEventListener("click", () => openFile(m.file, { line: m.line }));
      li.appendChild(btn); ul.appendChild(li);
    });
    b.appendChild(ul);
  }
  // Put each uploaded figure where the paper expects it (e.g. figures/fig1.png), matching by file name
  $("figUploadInput").addEventListener("change", async (e) => {
    const files = [...e.target.files]; if (!files.length) return;
    const wanted = (ed.data.compile?.missing || []).map(m => m.name);
    const groups = {};
    for (const f of files) {
      const hit = wanted.find(w => w.split("/").pop().toLowerCase() === f.name.toLowerCase()) ||
                  wanted.find(w => w.split("/").pop().replace(/\.[^.]+$/, "").toLowerCase() === f.name.replace(/\.[^.]+$/, "").toLowerCase());
      const folder = hit && hit.includes("/") ? hit.slice(0, hit.lastIndexOf("/")) : "";
      (groups[folder] ||= []).push(f);
    }
    for (const [folder, list] of Object.entries(groups)) await uploadFiles(list, folder);
    if (ed.data.latex.engine) compile();
  });

  function renderBanner() {
    renderMissing();
    const c = ed.data.compile, b = $("errBanner");
    const show = c && !ed.compiling && c.version === ed.data.version && c.main === ed.data.main && (c.errors.length || !c.pdf) && ed.data.latex.engine;
    b.hidden = !show; if (!show) return;
    b.innerHTML = "";
    const head = document.createElement("div");
    head.textContent = c.errors.length
      ? `LaTeX reported ${c.errors.length} error${c.errors.length > 1 ? "s" : ""}${c.pdf ? " (the PDF was still built)" : ""}:`
      : `The PDF couldn't be built. ${c.message || ""}`;
    b.appendChild(head);
    if (c.errors.length) {
      const ul = document.createElement("ul");
      c.errors.forEach(e => {
        const li = document.createElement("li");
        const label = `${e.file ? e.file + (e.line ? `, line ${e.line}` : "") + ": " : e.line ? `Line ${e.line}: ` : ""}${e.message}`;
        if (e.file && fileInfo(e.file)) {
          const btn = document.createElement("button"); btn.type = "button"; btn.textContent = label;
          btn.addEventListener("click", () => openFile(e.file, { line: e.line }));
          li.appendChild(btn);
        } else li.textContent = label;
        ul.appendChild(li);
      });
      b.appendChild(ul);
    }
    b.appendChild(makeBtn("Ask the chat to fix this", () => {
      const lines = c.errors.map(e => `- ${e.file ? e.file + " " : ""}${e.line ? `line ${e.line}: ` : ""}${e.message}`).join("\n") || c.message;
      send(`Fix the LaTeX errors so ${c.main} compiles:\n${lines}`);
    }));
  }

  async function compile() {
    if (ed.compiling || !ed.data) return;
    if (!ed.data.latex.engine) {
      if (!ed.data.latex.installing && ed.data.latex.can_install &&
          confirm("Compiling needs a LaTeX engine, which isn't set up yet.\n\nDownload Tectonic now? It's free, about 21 MB from GitHub, and only needed once. The first compile then downloads the LaTeX packages it needs, which can take a few minutes.")) {
        setupLatex();
      } else { renderSetupBanner(); }
      return;
    }
    ed.compiling = true; renderPdf(); renderBanner(); $("compileBtn").disabled = true;
    const id = ed.id;
    try {
      const d = await api("/compile");
      if (ed.id === id) ed.data = d;
    } catch (err) { if (ed.id === id) flashPill(err.message); }
    ed.compiling = false;
    if (ed.id === id) renderEditor();
  }
  $("compileBtn").addEventListener("click", async () => { if (ed.dirty) await saveFile(true); else compile(); showPdf(); });

  async function setupLatex() {
    try { const r = await fetch("/api/latex/setup", { method: "POST" }); ed.data.latex = await r.json(); } catch {}
    renderPdf();
    clearInterval(ed.installPoll);
    ed.installPoll = setInterval(async () => {
      const lt = await fetch("/api/latex").then(r => r.json()).catch(() => null);
      if (!lt || !ed.data) return;
      ed.data.latex = lt;
      if (!lt.installing) { clearInterval(ed.installPoll); renderEditor(); if (lt.engine) compile(); }
      else renderPdf();
    }, 1500);
  }

  // ----- chat attachments -----
  $("attachBtn").addEventListener("click", () => { $("attachInput").value = ""; $("attachInput").click(); });
  $("attachInput").addEventListener("change", (e) => {
    for (const file of e.target.files) {
      const a = { name: file.name, text: "", status: "reading" };
      ed.attachments.push(a);
      extractFile(file).then(d => { a.text = d.text; a.status = "ready"; })
        .catch(err => { a.status = "error"; a.error = err.message; })
        .finally(renderAttachments);
    }
    renderAttachments();
  });
  function renderAttachments() {
    const box = $("attChips"); box.innerHTML = "";
    box.hidden = !ed.attachments.length;
    ed.attachments.forEach((a, i) => {
      const chip = document.createElement("span"); chip.className = `att-chip${a.status === "error" ? " bad" : ""}`;
      chip.innerHTML = `<span></span><button type="button" aria-label="Remove ${escapeHtml(a.name)}">×</button>`;
      chip.firstChild.textContent = a.status === "reading" ? `${a.name} (reading…)` : a.status === "error" ? `${a.name}: ${a.error}` : a.name;
      chip.title = chip.firstChild.textContent;
      chip.lastChild.addEventListener("click", () => { ed.attachments.splice(i, 1); renderAttachments(); });
      box.appendChild(chip);
    });
  }

  // ----- chat -----
  function autoGrow() { const t = $("prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; }
  $("prompt").addEventListener("input", autoGrow);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!ed.busy) $("composer").requestSubmit(); }
  });
  $("edModel").addEventListener("change", () => store.set("editor_model", $("edModel").value.trim()));
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (ed.busy) { stopChat(); return; }
    const text = $("prompt").value.trim(); if (text) send(text);
  });

  function setBusy(on) {
    ed.busy = on;
    const b = $("sendBtn");
    b.setAttribute("aria-label", on ? "Stop" : "Send");
    b.innerHTML = on
      ? '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
  }

  let liveTimer = null;
  function pollLive() {
    clearTimeout(liveTimer);
    const id = ed.id;
    liveTimer = setTimeout(async () => {
      if (!ed.busy || ed.id !== id) return;
      try {
        const s = await apiGet("/chat/status");
        const box = $("msgs"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
        if (s.busy) {
          const lt = $("liveText"), st = $("liveStage");
          if (lt && s.reply !== undefined) lt.textContent = s.reply;
          if (st && !st.dataset.stopping) st.textContent = s.stage || "Working…";
          if (atBottom) scrollChat();
        } else if (ed.busy && !ed.waitingReply) {
          // Request finished in another tab or after a reload
          ed.data = await apiGet(""); setBusy(false); renderEditor(); await refreshOpen(); return;
        }
      } catch {}
      pollLive();
    }, 600);
  }

  async function send(text) {
    if (ed.busy || !ed.data) return;
    if (ed.attachments.some(a => a.status === "reading")) { alert("Wait until the attached files have been read."); return; }
    if (ed.dirty && !(await saveFile(false))) return;
    const id = ed.id, selection = ed.selection, selFile = ed.selFile, before = ed.data.version;
    const atts = ed.attachments.filter(a => a.status === "ready").map(a => ({ name: a.name, text: a.text }));
    ed.data.messages.push({ role: "user", text, selection: selection || undefined, selection_file: selFile || undefined,
                            attachments: atts.length ? atts.map(a => a.name) : undefined });
    $("prompt").value = ""; autoGrow(); setSelection(""); ed.attachments = []; renderAttachments();
    setBusy(true); ed.waitingReply = true; renderMessages(); pollLive();
    if (isSmall()) setPane("chat");
    try {
      const d = await api("/chat", {
        message: text, selection, open_path: selFile || ed.open || "", attachments: atts,
        model: $("edModel").value.trim(),
        api_key: $("api_key").value.trim() || store.get("api_key") || "",
      });
      if (ed.id === id) ed.data = d;
    } catch (err) {
      if (ed.id === id) ed.data.messages.push({ role: "assistant", text: netMessage(err), error: true });
    }
    ed.waitingReply = false;
    clearTimeout(liveTimer);
    setBusy(false);
    if (ed.id !== id) return;
    renderEditor();
    await refreshOpen();
    if (ed.data.version !== before && ed.data.latex.engine) compile();
  }
  async function stopChat() {
    $("sendBtn").disabled = true;
    await api("/stop").catch(() => {});
    $("sendBtn").disabled = false;
    const el = $("liveStage"); if (el) { el.dataset.stopping = "1"; el.textContent = "Stopping…"; }
  }

  init();
})();
