"use strict";
/* PDF Narrator desktop UI. Talks to Python through window.pywebview.api (webui/api.py). */

const S = {
  view: "library", bookId: null, stage: "review",
  overview: null, book: null,
  chapterId: null, text: "", tips: [], saveState: "saved", saveTimer: null, loadedChapter: null,
  languages: [], voices: [], voiceLanguage: null, voiceFilter: "all",
  paragraphIndex: 0, paragraph: "", samples: {}, speedDraft: null,
  playing: null, previewId: null, tableFilter: "all", exportFormat: null,
  settingsTab: "general", activitySeq: 0, activity: [], drawer: false, logFilter: "all",
  diagnostics: null, modal: null, confirmRemove: false,
};

// ---------------------------------------------------------------- helpers

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (id, size) => `<svg class="i"${size ? ` style="width:${size}px;height:${size}px"` : ""} aria-hidden="true"><use href="#${id}"/></svg>`;
const player = () => $("#player");

function hash(text) { let h = 0; for (const c of String(text)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }
function gradient(seed, a = 78, b = 42) { const hue = hash(seed) % 360; return `linear-gradient(160deg,hsl(${hue} 80% ${a}%),hsl(${(hue + 30) % 360} 60% ${b}%))`; }
function fmtDur(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${String(Math.round((seconds % 3600) / 60)).padStart(2, "0")}m`;
  if (seconds >= 60) return `${Math.round(seconds / 60)} min`;
  return `${seconds} s`;
}
function fmtClock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}
function tidy(text) { return String(text).replace(/\s+([,.;:!?’”)])/g, "$1").replace(/([“(])\s+/g, "$1"); }
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

async function call(name, ...args) {
  try {
    return await window.pywebview.api[name](...args);
  } catch (error) {
    toast(error && error.message ? error.message : String(error), true);
    throw error;
  }
}

function toast(text, isError = false) {
  const box = document.createElement("div");
  box.className = "toast" + (isError ? " err" : "");
  box.innerHTML = `${icon(isError ? "alert" : "check")}<span>${esc(text)}</span>`;
  $("#toasts").appendChild(box);
  setTimeout(() => box.remove(), isError ? 7000 : 4000);
}

function applyTheme() {
  const theme = S.overview?.settings?.theme || "light";
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

// ---------------------------------------------------------------- derived state

function activeJob() { return S.overview?.jobs?.active || null; }
function jobFor(bookId) {
  const active = activeJob();
  if (active && active.book_id === bookId) return active;
  return (S.overview?.jobs?.queued || []).find((job) => job.book_id === bookId) || null;
}
function busy(book) { const job = book && jobFor(book.id); return job && (job.kind === "create" || job.kind === "import"); }

function bookBadge(book) {
  const job = jobFor(book.id), s = book.summary;
  if (job && job.kind === "import") return `<span class="badge b-run"><span class="spin"></span>Reading book…</span>`;
  if (book.state === "importing") return `<span class="badge b-q">Waiting to read</span>`;
  if (book.state === "error") return `<span class="badge b-err">${icon("alert", 12)}Couldn't read</span>`;
  if (job && job.kind === "create") {
    if (!job.state || job.state === "queued") return `<span class="badge b-q">Waiting in queue</span>`;
    if (job.state === "paused") return `<span class="badge b-w">Paused · ${s.done} of ${s.included}</span>`;
    return `<span class="badge b-run">Creating · ${s.done} of ${s.included}</span>`;
  }
  if (job && job.kind === "export") return `<span class="badge b-run"><span class="spin"></span>Exporting…</span>`;
  if (!s) return "";
  if (s.included && s.done === s.included) return book.exported
    ? `<span class="badge b-ok">${icon("check", 12)}Exported · ${fmtDur(s.seconds)}</span>`
    : `<span class="badge b-ok">${icon("check", 12)}Ready to export · ${fmtDur(s.seconds)}</span>`;
  if (s.done) return `<span class="badge b-w">Partly created · ${s.done} of ${s.included}</span>`;
  return `<span class="badge b-q">Ready to narrate · ~${fmtDur(s.seconds)}</span>`;
}

function coverHtml(book, cls = "") {
  if (book.cover_url) return `<div class="cover ${cls}" style="background-image:url('${esc(book.cover_url)}')"></div>`;
  return `<div class="cover ${cls}" style="background:${gradient(book.title)}">${esc(book.title.slice(0, 40))}</div>`;
}

// ---------------------------------------------------------------- navigation

async function go(view, options = {}) {
  await flushSave();
  S.view = view;
  if (view === "book") {
    if (options.bookId && options.bookId !== S.bookId) {
      S.bookId = options.bookId; S.chapterId = null; S.loadedChapter = null; S.previewId = null;
      S.exportFormat = null; S.paragraph = ""; S.paragraphIndex = 0; S.speedDraft = null; S.confirmRemove = false;
    }
    S.book = await call("book", S.bookId);
    S.stage = options.stage || S.stage;
    if (!options.stage) S.stage = defaultStage(S.book);
    if (S.stage === "voice") await loadVoices();
  }
  render();
  window.scrollTo(0, 0);
}

function defaultStage(book) {
  if (book.state !== "ready") return "review";
  const job = jobFor(book.id);
  if (job && job.kind === "create") return "create";
  const s = book.summary;
  if (s && s.included && s.done === s.included) return "export";
  if (s && s.done) return "create";
  return "review";
}

async function goStage(stage) {
  await flushSave();
  S.stage = stage;
  if (stage === "voice") await loadVoices();
  render();
  window.scrollTo(0, 0);
}

// ---------------------------------------------------------------- render root

function render() {
  applyTheme();
  const screens = { library: libraryScreen, book: bookScreen, queue: queueScreen, settings: settingsScreen };
  $("#app").innerHTML = navHtml() + `<main id="screen">${(screens[S.view] || libraryScreen)()}</main>` + layerHtml();
  afterRender();
}

function renderLive() {
  // Refresh only parts without user input, so typing and sliders are never interrupted.
  applyTheme();
  const nav = $(".nav");
  if (nav) nav.outerHTML = navHtml();
  if (S.view === "library" || S.view === "queue" || (S.view === "book" && (S.stage === "create" || S.stage === "export"))) {
    $("#screen").innerHTML = ({ library: libraryScreen, queue: queueScreen, book: bookScreen })[S.view]();
    afterRender();
  } else if (S.view === "book") {
    const head = $("#bookhead");
    if (head) head.outerHTML = bookHeadHtml(S.book);
  }
  const layer = $("#layer");
  if (layer && S.drawer && !S.modal) { const log = $("#log"); const atBottom = !log || log.scrollTop + log.clientHeight >= log.scrollHeight - 30; layer.outerHTML = layerHtml(); const fresh = $("#log"); if (fresh && atBottom) fresh.scrollTop = fresh.scrollHeight; }
}

function navHtml() {
  const jobs = S.overview?.jobs, count = jobs ? (jobs.active ? 1 : 0) + jobs.queued.length : 0;
  const errors = S.activity.some((entry) => entry.level === "error" && entry.seq > (S.seenErrors || 0));
  const processor = S.overview?.processor?.label || "…";
  const tab = (view, label, extra = "") => `<button data-act="nav" data-view="${view}" class="${S.view === view ? "on" : ""}">${label}${extra}</button>`;
  return `<nav class="nav">
    <div class="brand"><span class="logo">${icon("wave", 14)}</span>PDF Narrator</div>
    <div class="links">${tab("library", "Library")}${tab("queue", "Queue", count ? `<span class="count">${count}</span>` : "")}${tab("settings", "Settings")}</div>
    <div class="right">
      <button class="chip" data-act="drawer" title="Activity"><span class="dot ${errors ? "err" : jobs?.active ? "run" : ""}"></span>${esc(processor)} · Activity</button>
      <button class="btn primary" data-act="choose-files">${icon("plus")}New audiobook</button>
    </div></nav>`;
}

// ---------------------------------------------------------------- library

function libraryScreen() {
  const books = S.overview?.books || [];
  const working = books.filter((book) => jobFor(book.id)).length;
  return `<div class="wrap">
    <div class="lhead"><div><h1>Your audiobooks</h1><div class="muted">${plural(books.length, "book")}${working ? ` · ${working} in progress` : ""}</div></div></div>
    <div class="drop">
      <div class="ic">${icon("up", 24)}</div>
      <div><b>Turn a book into an audiobook</b><span class="muted">Drop a PDF, EPUB, TXT or HTML file here. Everything stays on your computer.</span></div>
      <div class="acts"><button class="btn" data-act="choose-folder">${icon("folder")}Add a folder</button><button class="btn primary" data-act="choose-files">Choose file</button></div>
    </div>
    ${books.length ? `<div class="books">${books.map(bookCard).join("")}</div>`
      : `<div class="empty"><b>No audiobooks yet</b>Add a book to get started.</div>`}
  </div>`;
}

function bookCard(book) {
  const job = activeJob(), s = book.summary;
  const running = job && job.book_id === book.id && job.kind === "create" && s;
  const pct = running ? Math.round(100 * (s.done + (job.chapter_progress || 0)) / Math.max(s.included, 1)) : 0;
  return `<button class="card book" data-act="open" data-id="${esc(book.id)}">${coverHtml(book)}
    <div class="grow"><div class="t">${esc(book.title)}</div><div class="a">${esc(book.author || book.format.toUpperCase())}</div>
    ${bookBadge(book)}${running ? `<div class="pbar"><i style="width:${pct}%"></i></div>` : ""}</div></button>`;
}

// ---------------------------------------------------------------- book

function bookScreen() {
  const book = S.book;
  if (!book) return "";
  const stages = { review: reviewStage, voice: voiceStage, create: createStage, export: exportStage };
  return `<div class="wrap">${bookHeadHtml(book)}${(stages[S.stage] || reviewStage)(book)}</div>`;
}

function bookHeadHtml(book) {
  const s = book.summary, job = jobFor(book.id), ready = book.state === "ready";
  const lang = S.languages.find((item) => item.code === book.language)?.label || "";
  const running = job && job.kind === "create" && job.state && job.state !== "queued";
  const stage = (id, n, title, sub, done) => `<button class="stage ${S.stage === id ? "on" : ""} ${done && S.stage !== id ? "done" : ""}" data-act="stage" data-stage="${id}" ${!ready && id !== "review" ? "disabled" : ""}>
    <span class="n">${done && S.stage !== id ? icon("check", 14) : n}</span><div class="grow"><b>${title}</b><small>${sub}</small></div></button>`;
  const createSub = running ? `${job.chapter_index || 0} of ${job.chapter_count || "…"} · ${job.state === "paused" ? "paused" : "running"}`
    : s ? `${s.done} of ${s.included} done` : "Waiting for text";
  return `<div id="bookhead">
    <div class="bhead">
      <button class="icon-btn" data-act="nav" data-view="library" title="Back to library">${icon("left")}</button>
      ${coverHtml(book, "lg")}
      <div class="grow"><h1>${esc(book.title)}</h1><div class="muted">${[book.author, book.format.toUpperCase(), lang, s ? plural(s.total, "section") : "", s ? `~${s.words.toLocaleString("en-US")} words` : ""].filter(Boolean).map(esc).join(" · ")}</div></div>
      <div class="acts"><button class="btn" data-act="details">${icon("edit")}Book details</button></div>
    </div>
    <div class="pipe">
      ${stage("review", 1, "Check text", s ? `${s.included} of ${s.total} kept` : book.state === "error" ? "Needs attention" : "Reading…", ready)}
      ${stage("voice", 2, "Pick a voice", book.voice_name ? `${esc(book.voice_name)} · ${book.speed}×` : "—", ready)}
      ${stage("create", 3, "Create audio", createSub, s && s.included && s.done === s.included)}
      ${stage("export", 4, "Export", book.export ? `Exported ${book.export.format.toUpperCase()}` : "M4B or MP3", !!book.export)}
    </div></div>`;
}

function stateNotice(book) {
  const job = jobFor(book.id);
  if (book.state === "importing" || (job && job.kind === "import"))
    return `<div class="notice info"><span class="spin"></span><div><b>Reading your book…</b> Finding chapters and cleaning the text. This usually takes a few seconds.</div></div>`;
  if (book.state === "error")
    return `<div class="notice err">${icon("alert")}<div><b>Something went wrong.</b> ${esc(book.error || "")}</div><button class="btn sm" data-act="retry">${icon("refresh", 14)}Try again</button></div>`;
  return "";
}

// --- 1 check text

function reviewStage(book) {
  if (book.state !== "ready") return stateNotice(book);
  const chapters = book.chapters, s = book.summary;
  if (!S.chapterId || !chapters.some((c) => c.id === S.chapterId)) S.chapterId = (chapters.find((c) => c.included) || chapters[0]).id;
  const current = chapters.find((c) => c.id === S.chapterId);
  const locked = busy(book);
  const rows = chapters.map((c) => `<div class="crow ${c.included ? "" : "off"} ${c.id === S.chapterId ? "sel" : ""}" data-act="select-ch" data-id="${esc(c.id)}">
      <button class="cbx ${c.included ? "on" : ""}" data-act="toggle-ch" data-id="${esc(c.id)}" title="${c.included ? "Skip" : "Narrate"} this section" aria-pressed="${c.included}">${c.included ? icon("check", 12) : ""}</button>
      <span class="t">${esc(c.title)}</span><span class="s">${c.included ? "~" + fmtDur(c.seconds) : plural(c.words, "word")}</span></div>`).join("");
  const tip = S.tips[0];
  const saveLabel = { saved: "Saved automatically", saving: "Saving…", unsaved: "Unsaved changes", error: "Not saved" }[S.saveState];
  return `${book.error ? `<div class="notice warn">${icon("alert")}<div>${esc(book.error)}</div></div>` : ""}
  ${locked ? `<div class="notice info">${icon("alert")}<div>Audio is being created for this book. Pause and stop it in <b>Create audio</b> to edit the text.</div></div>` : ""}
  <div class="rv">
    <div class="card">
      <div class="ch"><h2>Chapters</h2><span class="muted small">${s.included} of ${s.total} kept</span>
        <button class="btn sm" style="margin-left:auto" data-act="skip-front" title="Skip covers, copyright pages, contents and very short sections">${icon("sparkle", 14)}Skip front matter</button></div>
      <div class="clist">${rows}</div>
    </div>
    <div class="card">
      <div class="ch"><h2 class="grow" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(current.title)}</h2><span class="muted small" id="save-state">${saveLabel}</span>
        <button class="btn sm" data-act="hear-paragraph">${S.playing?.kind === "paragraph" ? icon("stop", 12) + "Stop" : icon("play", 12) + "Hear this paragraph"}</button></div>
      ${tip ? `<div class="tip">${icon("alert")}<div><b>Possible page header.</b> “${esc(tip.line)}” repeats ${tip.count} times in this chapter.</div><button class="btn sm" data-act="remove-line" ${locked ? "disabled" : ""}>Remove all</button></div>` : ""}
      <textarea id="editor" class="editor" spellcheck="true" ${locked ? "readonly" : ""} aria-label="Chapter text"></textarea>
    </div>
  </div>
  <div class="footer-acts"><span class="muted">About ${fmtDur(s.seconds)} of audio.</span>
    <button class="btn primary big" data-act="stage" data-stage="voice">Continue to voice${icon("arrow")}</button></div>`;
}

async function loadChapter() {
  if (!S.book || S.book.state !== "ready" || S.loadedChapter === S.chapterId) return;
  const id = S.chapterId;
  const data = await call("chapter_text", S.bookId, id);
  if (S.chapterId !== id) return;
  S.text = data.text; S.tips = data.tips; S.loadedChapter = id; S.saveState = "saved";
  render();
}

function scheduleSave() {
  S.saveState = "unsaved";
  const label = $("#save-state");
  if (label) label.textContent = "Unsaved changes";
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(flushSave, 800);
}

async function flushSave() {
  clearTimeout(S.saveTimer);
  if (S.saveState !== "unsaved" || !S.loadedChapter) return;
  const label = $("#save-state");
  S.saveState = "saving";
  if (label) label.textContent = "Saving…";
  try {
    await call("save_chapter", S.bookId, S.loadedChapter, S.text);
    S.saveState = "saved";
    S.book = await call("book", S.bookId);
  } catch { S.saveState = "error"; }
  const fresh = $("#save-state");
  if (fresh) fresh.textContent = { saved: "Saved automatically", error: "Not saved" }[S.saveState] || "";
}

// --- 2 pick a voice

async function loadVoices() {
  if (!S.languages.length) S.languages = await call("languages");
  const language = S.book.language;
  if (S.voiceLanguage !== language) { S.voices = await call("voices", language); S.voiceLanguage = language; }
  if (!S.paragraph) S.paragraph = await call("paragraph", S.bookId, S.paragraphIndex);
}

function voiceStage(book) {
  if (book.state !== "ready") return stateNotice(book);
  const shown = S.voices.filter((v) => S.voiceFilter === "all" || v.gender === S.voiceFilter);
  const speed = S.speedDraft ?? book.speed, locked = busy(book);
  const anyDone = book.summary.done > 0;
  const card = (v) => {
    const sample = S.samples[v.id], playing = S.playing?.kind === "voice" && S.playing.id === v.id;
    const bars = Array.from({ length: 28 }, (_, j) => `<i style="height:${25 + Math.abs(Math.sin((j + hash(v.id) % 9) * 1.3)) * 75}%"></i>`).join("");
    return `<div class="card vc ${book.voice === v.id ? "sel" : ""}" data-act="pick-voice" data-id="${v.id}" role="button" tabindex="0">
      <div class="vtop"><span class="av" style="background:${gradient(v.id, 82, 50)}"></span><div><b>${esc(v.name)}</b><small>${v.gender === "female" ? "Female" : "Male"} · ${esc(v.language_label)}</small></div>
      ${v.grade ? `<span class="grade" title="Quality grade from the Kokoro voice list">${esc(v.grade)}</span>` : ""}</div>
      <div class="vplay"><button class="pbtn" data-act="play-voice" data-id="${v.id}" title="Hear ${esc(v.name)}">${sample?.state === "running" ? '<span class="spin"></span>' : icon(playing ? "stop" : "play", 13)}</button>
      <div class="mwave ${playing ? "playing" : ""}">${bars}</div></div></div>`;
  };
  const selected = S.voices.find((v) => v.id === book.voice);
  return `${locked ? `<div class="notice info">${icon("alert")}<div>Audio is being created. Stop it to change the voice or speed.</div></div>` : ""}
  ${anyDone ? `<div class="notice warn">${icon("alert")}<div>Changing the voice or speed marks created chapters for a redo.</div></div>` : ""}
  <div class="vgrid">
    <div>
      <div class="filters">
        <select data-act="language" aria-label="Book language" ${locked ? "disabled" : ""}>${S.languages.map((l) => `<option value="${l.code}" ${l.code === book.language ? "selected" : ""}>${esc(l.label)}</option>`).join("")}</select>
        <div class="seg">${[["all", "All"], ["female", "Female"], ["male", "Male"]].map(([k, l]) => `<button class="${S.voiceFilter === k ? "on" : ""}" data-act="voice-filter" data-filter="${k}">${l}</button>`).join("")}</div>
        <span class="muted small" style="margin-left:auto">Grades from the Kokoro voice list</span>
      </div>
      <div class="voices">${shown.map(card).join("") || `<div class="empty">No voices match.</div>`}</div>
    </div>
    <div class="side">
      <div class="card panel"><h2>Hear it with your book</h2>
        <p class="quote">“${esc(tidy(S.paragraph))}”</p>
        <button class="btn" style="width:100%" data-act="next-paragraph">Use another paragraph</button></div>
      <div class="card panel"><h2>Reading speed</h2>
        <input type="range" id="speed" min="0.7" max="1.5" step="0.05" value="${speed}" style="width:100%" ${locked ? "disabled" : ""} aria-label="Reading speed">
        <div class="sl"><span>Slower</span><b style="color:var(--text)" id="speed-label">${Number(speed).toFixed(2).replace(/0$/, "")}×</b><span>Faster</span></div></div>
      ${selected ? `<div class="card panel row"><span class="av sm" style="background:${gradient(selected.id, 82, 50)}"></span><div><b>${esc(selected.name)}</b><div class="muted small">Selected for this book</div></div></div>` : ""}
    </div>
  </div>
  <div class="footer-acts"><button class="btn" data-act="stage" data-stage="review">${icon("left")}Back</button>
    <button class="btn primary big" data-act="create">Create audiobook · ~${fmtDur(book.summary.seconds)} of audio${icon("arrow")}</button></div>`;
}

async function playVoice(voiceId) {
  if (S.playing?.kind === "voice" && S.playing.id === voiceId) return stopAudio();
  const speed = S.speedDraft ?? S.book.speed;
  let state = await call("sample", S.bookId, voiceId, speed, S.paragraph);
  S.samples[voiceId] = state;
  render();
  while (state && state.state === "running") {
    await new Promise((r) => setTimeout(r, 350));
    state = await call("sample_state", state.key);
  }
  S.samples[voiceId] = state;
  if (state?.state === "ready") playUrl(state.url, { kind: "voice", id: voiceId });
  else { toast(state?.error || "The sample could not be created.", true); render(); }
}

async function hearParagraph() {
  if (S.playing?.kind === "paragraph") return stopAudio();
  const editor = $("#editor");
  const text = editor ? paragraphAt(editor.value, editor.selectionStart) : S.text.slice(0, 400);
  if (!text.trim()) return toast("Place the cursor in a paragraph first.", true);
  S.playing = { kind: "paragraph", id: "pending" };
  render();
  let state = await call("sample", S.bookId, S.book.voice, S.book.speed, text);
  while (state && state.state === "running") { await new Promise((r) => setTimeout(r, 350)); state = await call("sample_state", state.key); }
  if (state?.state === "ready" && S.playing?.kind === "paragraph") playUrl(state.url, { kind: "paragraph", id: state.key });
  else { S.playing = null; render(); }
}

function paragraphAt(text, position) {
  const start = text.lastIndexOf("\n", Math.max(0, position - 1)) + 1;
  let end = text.indexOf("\n", position); if (end < 0) end = text.length;
  let paragraph = text.slice(start, end).trim();
  if (!paragraph) paragraph = text.slice(position, position + 400);
  return paragraph.slice(0, 600);
}

// --- 3 create audio

function createStage(book) {
  if (book.state !== "ready") return stateNotice(book);
  const s = book.summary, job = jobFor(book.id), active = activeJob();
  const mine = active && active.book_id === book.id && active.kind === "create" ? active : null;
  const pending = s.waiting + s.redo;
  const pct = Math.round(100 * (s.done + (mine?.chapter_progress || 0)) / Math.max(s.included, 1));
  const included = book.chapters.filter((c) => c.included);
  const filtered = included.filter((c) => S.tableFilter === "all" || (S.tableFilter === "done" ? c.status === "done" : S.tableFilter === "waiting" ? c.status === "waiting" : c.status === "redo"));
  if (!S.previewId || !included.some((c) => c.id === S.previewId && c.audio_url)) S.previewId = [...included].reverse().find((c) => c.audio_url)?.id || null;
  const preview = included.find((c) => c.id === S.previewId);
  const statusBadge = (c) => {
    if (mine && mine.chapter_id === c.id) return `<span class="badge b-run">${mine.state === "paused" ? "Paused" : "Creating"} · ${Math.round((mine.chapter_progress || 0) * 100)}%</span>`;
    return { done: `<span class="badge b-ok">${icon("check", 12)}Done</span>`, redo: `<span class="badge b-w">Needs a redo</span>`, waiting: `<span class="badge b-q">Waiting</span>` }[c.status] || "";
  };
  const index = new Map(book.chapters.map((c, i) => [c.id, i + 1]));
  const rows = filtered.map((c) => `<tr><td class="numc">${index.get(c.id)}</td><td>${mine?.chapter_id === c.id ? `<b>${esc(c.title)}</b>` : esc(c.title)}</td>
    <td class="r">${c.status === "done" ? fmtClock(c.seconds) : "~" + fmtDur(c.seconds)}</td><td>${statusBadge(c)}</td>
    <td>${c.audio_url ? `<button class="icon-btn ${S.previewId === c.id && S.playing?.kind === "chapter" ? "on" : ""}" data-act="preview" data-id="${esc(c.id)}" title="Preview">${icon(S.previewId === c.id && S.playing?.kind === "chapter" ? "pause" : "play")}</button>` : ""}</td></tr>`).join("");

  let control;
  if (mine) {
    const preparing = !mine.chapter_index;
    control = `<div class="card panel"><h2>${mine.state === "paused" ? "Paused" : preparing ? "Getting ready" : "Creating audio"}</h2>
      <div class="muted small" style="margin-bottom:12px">${preparing ? "Loading the voice engine. The first time, it downloads the voice model (about 350 MB)." : "You can keep using the app while this runs."}</div>
      <div class="row">${mine.state === "paused" ? `<button class="btn grow" data-act="resume">${icon("play")}Resume</button>` : `<button class="btn grow" data-act="pause">${icon("pause")}Pause</button>`}
      <button class="btn grow" data-act="stop">${icon("stop", 12)}Stop</button></div></div>`;
  } else if (job) {
    control = `<div class="card panel"><h2>Waiting in queue</h2><div class="muted small" style="margin-bottom:12px">Another book is being created. This one starts next.</div><button class="btn" data-act="stop">Remove from queue</button></div>`;
  } else if (pending) {
    control = `<div class="card panel"><h2>${s.done ? "Continue" : "Ready to create"}</h2>
      <div class="muted small" style="margin-bottom:12px">${s.redo ? `${plural(s.redo, "chapter")} changed since its audio was made. ` : ""}${plural(pending, "chapter")} to create with ${esc(book.voice_name)}.</div>
      <button class="btn primary big" style="width:100%" data-act="create">${icon("play")}${s.done ? `Create ${plural(pending, "chapter")}` : "Create audiobook"}</button></div>`;
  } else {
    control = `<div class="card panel"><h2>All chapters are ready</h2><div class="muted small" style="margin-bottom:12px">Listen to any chapter, then export your audiobook.</div>
      <button class="btn primary big" style="width:100%" data-act="stage" data-stage="export">Continue to export${icon("arrow")}</button></div>`;
  }
  const playingPreview = S.playing?.kind === "chapter" && S.playing.id === S.previewId;
  const bars = Array.from({ length: 44 }, (_, k) => `<i data-k="${k}" style="height:${20 + Math.abs(Math.sin(k * 1.7) * 60 + Math.cos(k * 0.6) * 20)}%"></i>`).join("");
  return `${book.error ? `<div class="notice err">${icon("alert")}<div><b>The last run stopped with an error.</b> ${esc(book.error)}</div></div>` : ""}
  <div class="stats">
    <div class="card stat"><div class="k">${icon("list")}Chapters done</div><div class="v">${s.done} <span class="muted" style="font-size:16px">/ ${s.included}</span></div><div class="pbar" style="margin-top:10px"><i style="width:${pct}%"></i></div></div>
    <div class="card stat"><div class="k">${icon("clock")}Time remaining</div><div class="v">${mine?.seconds_left ? fmtDur(mine.seconds_left) : pending ? "—" : "Done"}</div><div class="s">${mine?.seconds_left ? "Done around " + new Date(Date.now() + mine.seconds_left * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : mine ? "Measuring…" : pending ? "Starts when you create" : "Every chapter is ready"}</div></div>
    <div class="card stat"><div class="k">${icon("zap")}Speed</div><div class="v">${mine?.speed_ratio ? Math.round(mine.speed_ratio) + "×" : "—"}</div><div class="s">${mine?.speed_ratio ? "faster than listening" : esc(S.overview?.processor?.label || "")}</div></div>
    <div class="card stat"><div class="k">${icon("headph")}Audiobook length</div><div class="v">${fmtDur(s.seconds)}</div><div class="s">~${s.words.toLocaleString("en-US")} words</div></div>
  </div>
  <div class="ggrid">
    <div class="card">
      <div class="ch"><h2>Chapters</h2><div class="seg" style="margin-left:auto">${[["all", "All"], ["done", "Done"], ["waiting", "Waiting"], ["redo", "Needs a redo"]].map(([k, l]) => `<button class="${S.tableFilter === k ? "on" : ""}" data-act="table-filter" data-filter="${k}">${l}</button>`).join("")}</div></div>
      <div class="tablewrap"><table><thead><tr><th>#</th><th>Chapter</th><th class="r">Length</th><th>Status</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="5" class="muted">Nothing here.</td></tr>`}</tbody></table></div>
    </div>
    <div class="side">${control}
      <div class="card panel"><div class="muted small">Preview${preview ? " · " + esc(preview.title) : ""}</div>
        <div class="wave" id="pv-wave">${bars}</div>
        <div class="times"><span id="pv-time">0:00</span><span>${preview ? fmtClock(preview.seconds) : "0:00"}</span></div>
        <div class="pc"><button class="icon-btn" data-act="seek" data-by="-15" title="Back 15 seconds" ${preview ? "" : "disabled"}>${icon("back15")}</button>
        <button class="bigplay" data-act="preview" data-id="${esc(S.previewId || "")}" ${preview ? "" : "disabled"} title="Play or pause">${icon(playingPreview ? "pause" : "play", 18)}</button>
        <button class="icon-btn" data-act="seek" data-by="15" title="Forward 15 seconds" ${preview ? "" : "disabled"}>${icon("fwd15")}</button></div>
        ${preview ? "" : `<div class="muted small" style="text-align:center;margin-top:8px">Finished chapters appear here.</div>`}</div>
    </div>
  </div>`;
}

// --- 4 export

function exportStage(book) {
  if (book.state !== "ready") return stateNotice(book);
  const s = book.summary, job = jobFor(book.id), ffmpeg = S.overview?.ffmpeg;
  const complete = s.included && s.done === s.included;
  const format = S.exportFormat || (ffmpeg ? S.overview?.settings?.export_format : "mp3") || "m4b";
  const exporting = job && job.kind === "export";
  const option = (id, title, text, disabled) => `<button class="opt ${format === id ? "on" : ""}" data-act="format" data-format="${id}" ${disabled ? "disabled" : ""}><b>${title}</b><small>${text}</small></button>`;
  return `${complete ? "" : `<div class="notice info">${icon("alert")}<div>Create every chapter before exporting. ${s.done} of ${s.included} are ready.</div><button class="btn sm" data-act="stage" data-stage="create">Go to Create audio</button></div>`}
  ${book.error && !exporting ? `<div class="notice err">${icon("alert")}<div>${esc(book.error)}</div></div>` : ""}
  <div class="ggrid">
    <div class="card panel">
      <h2>Export format</h2>
      <div class="out">${option("m4b", "Audiobook (M4B)", ffmpeg ? "One file with chapters, title and cover. Works in Apple Books and most audiobook apps." : "Needs FFmpeg, which is not installed.", !ffmpeg)}
        ${option("mp3", "MP3 per chapter", "One file per chapter. Plays everywhere.", false)}</div>
      <div class="plain" style="margin-top:8px">
        <div class="srow"><div class="l"><b>Title and author</b><small>${esc(book.title)}${book.author ? " · " + esc(book.author) : ""}</small></div><button class="btn" data-act="details">Edit</button></div>
        <div class="srow"><div class="l"><b>Cover</b><small>${book.cover_url ? "From the book" : "None found; the audiobook will have no cover"}</small></div>${book.cover_url ? coverHtml(book, "sm") : ""}</div>
        <div class="srow"><div class="l"><b>Saved in</b><small>${esc(book.folder)}/export</small></div><button class="btn" data-act="reveal" data-which="folder">${icon("folder")}Open folder</button></div>
      </div>
      <div class="footer-acts"><span class="muted small">${exporting ? "Exporting… this takes about a minute for a long book." : complete ? `~${fmtDur(s.seconds)} of audio in ${plural(s.included, "chapter")}.` : ""}</span>
        <button class="btn primary big" data-act="export" ${complete && !exporting && !busy(book) ? "" : "disabled"}>${exporting ? '<span class="spin"></span>Exporting…' : icon("dl") + (book.export ? "Export again" : "Export audiobook")}</button></div>
    </div>
    <div class="card panel"><h2>Finished file</h2>
      ${book.export ? `<div class="row" style="margin-bottom:12px"><span class="badge b-ok">${icon("check", 12)}${book.export.format.toUpperCase()}</span><span class="small" style="word-break:break-all">${esc(book.export.name)}</span></div>
        <div class="muted small" style="margin-bottom:14px">Exported ${new Date(book.export.created_at * 1000).toLocaleString("en-US")}</div>
        <button class="btn" data-act="reveal" data-which="export">${icon("folder")}Show file</button>`
      : `<div class="muted small">Your exported audiobook will appear here.</div>`}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- queue

function queueScreen() {
  const jobs = S.overview?.jobs || { active: null, queued: [] };
  const books = new Map((S.overview?.books || []).map((b) => [b.id, b]));
  const kinds = { import: "Reading book", create: "Creating audio", export: "Exporting" };
  const row = (job, active) => {
    const book = books.get(job.book_id);
    if (!book) return "";
    return `<div class="qrow">${coverHtml(book, "lg")}<div class="grow"><b>${esc(book.title)}</b>
      <div class="muted small">${kinds[job.kind]}${active ? ` · ${esc(job.message || "")}` : " · waiting"}</div>
      ${active && job.kind === "create" ? `<div class="pbar" style="margin-top:8px;max-width:420px"><i style="width:${Math.round(job.progress || 0)}%"></i></div>` : ""}</div>
      ${active && job.kind === "create" ? (job.state === "paused" ? `<button class="btn" data-act="resume">${icon("play")}Resume</button>` : `<button class="btn" data-act="pause">${icon("pause")}Pause</button>`) : ""}
      <button class="btn" data-act="stop-book" data-id="${esc(job.book_id)}">${active ? "Stop" : "Remove"}</button>
      <button class="btn ghost" data-act="open" data-id="${esc(job.book_id)}">Open</button></div>`;
  };
  const list = (jobs.active ? row(jobs.active, true) : "") + jobs.queued.map((job) => row(job, false)).join("");
  return `<div class="wrap"><h1 style="margin-bottom:6px">Queue</h1><div class="muted" style="margin-bottom:20px">Books are created one at a time, in this order.</div>
    <div class="card">${list || `<div class="empty"><b>Nothing in the queue</b>Books you create appear here.</div>`}</div></div>`;
}

// ---------------------------------------------------------------- settings

function settingsScreen() {
  const settings = S.overview?.settings || {}, processor = S.overview?.processor || {};
  const tab = (id, label) => `<button class="${S.settingsTab === id ? "on" : ""}" data-act="settings-tab" data-tab="${id}">${label}</button>`;
  const names = { cpu: "CPU", mps: "Apple GPU", cuda: "NVIDIA GPU" };
  let body;
  if (S.settingsTab === "engine") {
    body = `<div class="srow"><div class="l"><b>Processor</b><small>Automatic picks the fastest one${processor.label ? `. Detected: ${esc(processor.label)}` : ""}. Applies to the next book you create.</small></div>
      <select data-act="setting" data-key="device"><option value="auto" ${settings.device === "auto" ? "selected" : ""}>Automatic (recommended)</option>
      ${(processor.available || []).map((d) => `<option value="${d}" ${settings.device === d ? "selected" : ""}>${names[d]}</option>`).join("")}</select></div>
      <div class="srow"><div class="l"><b>Voice model</b><small>Kokoro 82M, running on this computer. It is downloaded once (about 350 MB).</small></div></div>`;
  } else if (S.settingsTab === "about") {
    body = `<div class="srow"><div class="l"><b>PDF Narrator</b><small>Free and open source · MIT license · github.com/mateogon/pdf-narrator</small></div></div>
      <div class="srow" style="display:block"><div class="l" style="margin-bottom:10px"><b>Diagnostics</b><small>Copy this into a GitHub issue if something goes wrong.</small></div>
      ${S.diagnostics ? `<textarea class="diag" readonly id="diag">${esc(S.diagnostics)}</textarea>` : `<button class="btn" data-act="diagnostics">Show diagnostics</button>`}</div>`;
  } else {
    body = `<div class="srow"><div class="l"><b>Library folder</b><small>${esc(settings.library_root || "")}</small></div><button class="btn" data-act="library-folder">Change</button></div>
      <div class="srow"><div class="l"><b>Default export</b><small>Format chosen first on the Export step</small></div>
      <select data-act="setting" data-key="export_format"><option value="m4b" ${settings.export_format === "m4b" ? "selected" : ""}>Audiobook (M4B)</option><option value="mp3" ${settings.export_format === "mp3" ? "selected" : ""}>MP3 per chapter</option></select></div>
      <div class="srow"><div class="l"><b>Appearance</b></div><div class="seg">${[["light", "Light"], ["dark", "Dark"], ["system", "System"]].map(([k, l]) => `<button class="${(settings.theme || "light") === k ? "on" : ""}" data-act="theme" data-theme="${k}">${l}</button>`).join("")}</div></div>`;
  }
  return `<div class="wrap"><h1 style="margin-bottom:20px">Settings</h1><div class="sgrid">
    <div class="snav">${tab("general", "General")}${tab("engine", "Voice engine")}${tab("about", "About")}</div>
    <div class="card">${body}</div></div></div>`;
}

// ---------------------------------------------------------------- overlays

function layerHtml() {
  let html = "";
  if (S.drawer) {
    const entries = S.activity.filter((e) => S.logFilter === "details" || (S.logFilter === "errors" ? e.level === "error" : e.level !== "technical"));
    html += `<aside class="drawer" aria-label="Activity"><div class="dh"><h2 class="grow">Activity</h2>
      <div class="seg">${[["all", "Summary"], ["errors", "Errors"], ["details", "Details"]].map(([k, l]) => `<button class="${S.logFilter === k ? "on" : ""}" data-act="log-filter" data-filter="${k}">${l}</button>`).join("")}</div>
      <button class="icon-btn" data-act="drawer" title="Close">${icon("x")}</button></div>
      <div class="log" id="log">${entries.map((e) => `<div class="${e.level}">${new Date(e.time * 1000).toLocaleTimeString()}  ${esc(e.text)}</div>`).join("") || '<div class="technical">Nothing yet.</div>'}</div></aside>`;
  }
  if (S.modal === "details" && S.book) {
    const book = S.book;
    html += `<div class="backdrop" data-act="close-modal"><div class="modal" role="dialog" aria-modal="true" aria-label="Book details">
      <div class="mh"><h2>Book details</h2></div>
      <div class="mb"><div class="field"><label for="f-title">Title</label><input class="input" id="f-title" value="${esc(book.title)}"></div>
      <div class="field"><label for="f-author">Author</label><input class="input" id="f-author" value="${esc(book.author)}"></div>
      <div class="muted small">Source file: ${esc(book.source_name)}</div></div>
      <div class="mf"><button class="btn danger" data-act="remove-book" style="margin-right:auto">${icon("trash", 14)}${S.confirmRemove ? "Click again to remove" : "Remove from library"}</button>
      <button class="btn" data-act="close-modal">Cancel</button><button class="btn primary" data-act="save-details">Save</button></div></div></div>`;
  }
  return `<div id="layer">${html}</div>`;
}

// ---------------------------------------------------------------- after render

function afterRender() {
  const editor = $("#editor");
  if (editor) {
    if (S.loadedChapter === S.chapterId) editor.value = S.text;
    else { editor.value = ""; editor.placeholder = "Loading…"; loadChapter(); }
    editor.addEventListener("input", () => { S.text = editor.value; scheduleSave(); });
  }
  const speed = $("#speed");
  if (speed) {
    speed.addEventListener("input", () => { S.speedDraft = Number(speed.value); $("#speed-label").textContent = `${S.speedDraft}×`; });
    speed.addEventListener("change", async () => {
      try { await call("update_book", S.bookId, { speed: Number(speed.value) }); S.book = await call("book", S.bookId); }
      finally { S.speedDraft = null; S.samples = {}; render(); }
    });
  }
  updatePreviewProgress();
}

// ---------------------------------------------------------------- audio

function playUrl(url, what) {
  const audio = player();
  audio.src = url;
  audio.play().catch(() => toast("Could not play the audio.", true));
  S.playing = what;
  render();
}

function stopAudio() { const audio = player(); audio.pause(); S.playing = null; render(); }

function updatePreviewProgress() {
  const audio = player(), wave = $("#pv-wave"), time = $("#pv-time");
  if (!wave || S.playing?.kind !== "chapter" || S.playing.id !== S.previewId) return;
  const fraction = audio.duration ? audio.currentTime / audio.duration : 0;
  const bars = wave.children, lit = Math.round(fraction * bars.length);
  for (let k = 0; k < bars.length; k++) bars[k].classList.toggle("p", k < lit);
  if (time) time.textContent = fmtClock(audio.currentTime);
}

function setupAudio() {
  const audio = player();
  audio.addEventListener("timeupdate", updatePreviewProgress);
  audio.addEventListener("ended", () => { S.playing = null; render(); });
}

async function previewChapter(chapterId) {
  const chapter = S.book.chapters.find((c) => c.id === chapterId);
  if (!chapter?.audio_url) return;
  const audio = player();
  if (S.playing?.kind === "chapter" && S.playing.id === chapterId) {
    if (audio.paused) { audio.play(); } else { audio.pause(); S.playing = { kind: "chapter-paused", id: chapterId }; render(); }
    return;
  }
  if (S.playing?.kind === "chapter-paused" && S.playing.id === chapterId) { audio.play(); S.playing = { kind: "chapter", id: chapterId }; render(); return; }
  S.previewId = chapterId;
  playUrl(chapter.audio_url, { kind: "chapter", id: chapterId });
}

// ---------------------------------------------------------------- actions

const actions = {
  nav: (el) => go(el.dataset.view),
  open: (el) => go("book", { bookId: el.dataset.id }),
  stage: (el) => goStage(el.dataset.stage),
  "choose-files": async () => { const ids = await call("choose_files"); if (ids.length) { toast(`Added ${plural(ids.length, "book")}. Reading the text…`); await refresh(); if (ids.length === 1) go("book", { bookId: ids[0], stage: "review" }); } },
  "choose-folder": async () => { const ids = await call("choose_folder"); if (ids.length) { toast(`Added ${plural(ids.length, "book")}.`); await refresh(); } },
  retry: async () => { await call("retry_import", S.bookId); await refresh(); },
  "select-ch": async (el) => { await flushSave(); S.chapterId = el.dataset.id; S.loadedChapter = null; render(); },
  "toggle-ch": async (el, event) => {
    event.stopPropagation();
    const chapter = S.book.chapters.find((c) => c.id === el.dataset.id);
    await call("set_included", S.bookId, [chapter.id], !chapter.included);
    S.book = await call("book", S.bookId); renderKeepEditor();
  },
  "skip-front": async () => { await call("skip_front_matter", S.bookId); S.book = await call("book", S.bookId); renderKeepEditor(); toast("Skipped covers, contents and very short sections."); },
  "remove-line": async () => {
    await flushSave();
    const data = await call("remove_line", S.bookId, S.loadedChapter, S.tips[0].line);
    S.text = data.text; S.tips = data.tips; S.book = await call("book", S.bookId); render(); toast("Removed the repeated line.");
  },
  "hear-paragraph": () => hearParagraph(),
  "pick-voice": async (el) => {
    if (busy(S.book) || el.dataset.id === S.book.voice) return;
    await call("update_book", S.bookId, { voice: el.dataset.id }); S.book = await call("book", S.bookId); render();
  },
  "play-voice": (el, event) => { event.stopPropagation(); playVoice(el.dataset.id); },
  "voice-filter": (el) => { S.voiceFilter = el.dataset.filter; render(); },
  "next-paragraph": async () => { S.paragraphIndex += 1; S.paragraph = await call("paragraph", S.bookId, S.paragraphIndex); S.samples = {}; render(); },
  create: async () => { await call("create", S.bookId); await refresh(); S.stage = "create"; render(); },
  pause: async () => { await call("pause"); await refresh(); },
  resume: async () => { await call("resume"); await refresh(); },
  stop: async () => { await call("stop", S.bookId); await refresh(); },
  "stop-book": async (el) => { await call("stop", el.dataset.id); await refresh(); },
  "table-filter": (el) => { S.tableFilter = el.dataset.filter; render(); },
  preview: (el) => previewChapter(el.dataset.id),
  seek: (el) => { const audio = player(); if (audio.src) audio.currentTime = Math.max(0, audio.currentTime + Number(el.dataset.by)); },
  format: (el) => { S.exportFormat = el.dataset.format; render(); },
  export: async () => {
    const format = S.exportFormat || (S.overview.ffmpeg ? S.overview.settings.export_format : "mp3");
    await call("export", S.bookId, format); await refresh();
  },
  reveal: (el) => call("reveal", S.bookId, el.dataset.which),
  details: () => { S.modal = "details"; S.confirmRemove = false; render(); setTimeout(() => $("#f-title")?.focus(), 0); },
  "close-modal": (el, event) => { if (el.tagName === "BUTTON" || event.target === el) { S.modal = null; render(); } },
  "save-details": async () => {
    await call("update_book", S.bookId, { title: $("#f-title").value, author: $("#f-author").value });
    S.modal = null; S.book = await call("book", S.bookId); await refresh();
  },
  "remove-book": async () => {
    if (!S.confirmRemove) { S.confirmRemove = true; render(); return; }
    await call("remove_book", S.bookId); S.modal = null; S.bookId = null; toast("Removed from the library."); await refresh(); go("library");
  },
  language: () => {},
  drawer: () => { S.drawer = !S.drawer; S.seenErrors = S.activity.length ? S.activity[S.activity.length - 1].seq : 0; render(); const log = $("#log"); if (log) log.scrollTop = log.scrollHeight; },
  "log-filter": (el) => { S.logFilter = el.dataset.filter; render(); const log = $("#log"); if (log) log.scrollTop = log.scrollHeight; },
  "settings-tab": (el) => { S.settingsTab = el.dataset.tab; render(); },
  theme: async (el) => { await call("update_settings", { theme: el.dataset.theme }); await refresh(); render(); },
  setting: () => {},
  "library-folder": async () => { const root = await call("choose_library_folder"); if (root) { toast("Library folder changed."); await refresh(); render(); } },
  diagnostics: async () => { S.diagnostics = await call("diagnostics"); render(); $("#diag")?.select(); },
};

function renderKeepEditor() {
  // Re-render while keeping the unsaved editor text in memory (it is restored by afterRender).
  const editor = $("#editor");
  if (editor) S.text = editor.value;
  render();
}

document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const handler = actions[el.dataset.act];
  if (handler) Promise.resolve(handler(el, event)).catch(() => {});
});

document.addEventListener("change", async (event) => {
  const el = event.target;
  if (el.dataset.act === "language") {
    const result = await call("update_book", S.bookId, { language: el.value }).catch(() => null);
    S.voiceLanguage = null; S.samples = {};
    S.book = await call("book", S.bookId);
    if (result?.reread) { toast("Reading the text again for this language…"); S.stage = "review"; S.loadedChapter = null; await refresh(); render(); return; }
    await loadVoices(); render();
  } else if (el.dataset.act === "setting") {
    await call("update_settings", { [el.dataset.key]: el.value }).catch(() => {});
    await refresh(); render();
  }
});

document.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") { event.preventDefault(); actions["choose-files"](); }
  else if (event.key === "Escape" && (S.modal || S.drawer)) { S.modal = null; S.drawer = false; render(); }
  else if (event.key === " " && !typing && S.view === "book" && S.stage === "create" && S.previewId) { event.preventDefault(); previewChapter(S.previewId); }
  else if (event.key === "Enter" && !typing && event.target.closest?.("[data-act=pick-voice]")) event.target.click();
});

let dragDepth = 0;
document.addEventListener("dragenter", (event) => { if (event.dataTransfer?.types?.includes("Files")) { dragDepth++; document.body.classList.add("dragging"); } });
document.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) document.body.classList.remove("dragging"); });
document.addEventListener("drop", () => { dragDepth = 0; document.body.classList.remove("dragging"); setTimeout(refresh, 400); setTimeout(refresh, 1200); });
document.addEventListener("dragover", (event) => event.preventDefault());

// ---------------------------------------------------------------- polling

let lastActive = null;
async function refresh() {
  const overview = await window.pywebview.api.state(S.activitySeq);
  S.overview = overview;
  if (overview.activity.length) {
    // The first load returns past history: show it in Activity, not as toasts.
    for (const entry of S.activitySeq ? overview.activity : []) {
      if (entry.level === "error") toast(entry.text, true);
      else if (entry.level === "success") toast(entry.text);
    }
    S.activity = S.activity.concat(overview.activity).slice(-800);
    S.activitySeq = S.activity[S.activity.length - 1].seq;
  }
  const active = overview.jobs.active;
  if (S.view === "book" && S.bookId) {
    if (!overview.books.some((b) => b.id === S.bookId)) { S.view = "library"; S.bookId = null; S.book = null; return; }
    const touched = jobFor(S.bookId) || (lastActive && lastActive.book_id === S.bookId) || S.book?.state === "importing";
    if (touched) {
      const before = S.book?.state;
      S.book = await window.pywebview.api.book(S.bookId);
      if (before !== S.book.state && S.stage === "review") { S.loadedChapter = null; render(); }
    }
  }
  lastActive = active;
}

async function loop() {
  try { await refresh(); renderLive(); } catch (error) { console.error(error); }
  const fast = S.overview?.jobs?.active || S.overview?.jobs?.queued?.length || S.book?.state === "importing";
  setTimeout(loop, fast ? 700 : 2000);
}

async function start() {
  setupAudio();
  S.languages = await call("languages");
  await refresh();
  render();
  setTimeout(loop, 700);
}

if (window.pywebview?.api) start();
else window.addEventListener("pywebviewready", start);
setTimeout(() => {
  if (!window.pywebview) $("#app").innerHTML = '<div class="wrap"><div class="empty"><b>Open PDF Narrator from the app.</b>This page runs inside the desktop window.</div></div>';
}, 3000);
