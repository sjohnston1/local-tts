// Video companion: plays a video from the library, follows its transcript (stored in SQLite),
// and gives Talk mode the context of what you've watched so far.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    pick: $("videoPick"), close: $("closeVideo"), status: $("videoStatus"),
    box: $("videoBox"), video: $("video"), micMode: $("videoMicMode"), autoResume: $("autoResume"),
    lines: $("videoLines"),
  };
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  const V = {
    id: null, title: "", folder: "", segments: [], status: null, progress: 0, duration: 0,
    pollTimer: null, lastPlayingAt: 0, pausedAt: 0, pausedByBot: false, resumeTimer: null, listeners: [],
  };
  ui.micMode.value = store.get("video.micMode", "paused");
  ui.micMode.onchange = () => store.set("video.micMode", ui.micMode.value);
  ui.autoResume.checked = store.get("video.autoResume", "1") === "1";
  ui.autoPlay = $("autoPlayVideo");
  ui.autoPlay.checked = store.get("video.autoPlay", "1") === "1";
  ui.autoPlay.onchange = () => store.set("video.autoPlay", ui.autoPlay.checked ? "1" : "0");
  ui.autoResume.onchange =() => store.set("video.autoResume", ui.autoResume.checked ? "1" : "0");

  const fmt = (t) => {
    t = Math.max(0, Math.floor(t || 0));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = String(t % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  };
  const playing = () => !ui.video.paused && !ui.video.ended;
  const setStatus = (t) => (ui.status.textContent = t);

  // ---------- picker (videos from the library) ----------
  function fillPicker() {
    const vids = (window.lib?.items || []).filter((i) => i.kind === "video" && !i.rel_path.split("/").pop().startsWith(".pending-")
                                                         && i.status !== "missing");
    const keep = ui.pick.value || (V.id ? String(V.id) : "");
    ui.pick.innerHTML = "";
    ui.pick.append(new Option(vids.length ? "Watch a video from your library…" : "No videos yet (add some in Library)", ""));
    const groups = {};
    for (const v of vids) (groups[v.folder || "Library"] ??= []).push(v);
    for (const [folder, list] of Object.entries(groups)) {
      const og = document.createElement("optgroup"); og.label = folder;
      for (const v of list) og.append(new Option(v.title + (v.status !== "ready" ? " (transcribing…)" : ""), v.id));
      ui.pick.append(og);
    }
    ui.pick.value = keep;
  }
  window.lib?.onChange(fillPicker);
  ui.pick.onchange = () => { if (ui.pick.value) openItem(Number(ui.pick.value)); else closeVideo(); };
  ui.close.onclick = closeVideo;

  async function openItem(id, startAt, autoplay = true) {
    if (V.id === id) { if (startAt != null) ui.video.currentTime = startAt; return; }
    closeVideo();
    try {
      const item = await (await fetch(`/api/items/${id}`)).json();
      Object.assign(V, { id, title: item.title, folder: item.folder, duration: item.duration || 0 });
      ui.pick.value = String(id);
      ui.video.src = `/api/items/${id}/file`;
      if (startAt != null) ui.video.addEventListener("loadedmetadata", () => (ui.video.currentTime = startAt), { once: true });
      ui.box.classList.remove("hidden"); ui.close.classList.remove("hidden");
      if (autoplay && ui.autoPlay.checked) ui.video.play().catch(() => {});   // blocked by the browser? then the play button still works
      setStatus("Loading transcript…");
      V.listeners.forEach((f) => f());
      poll();
    } catch (e) { setStatus("Couldn't open that video: " + e.message); }
  }
  function closeVideo() {
    const had = V.id !== null;
    clearTimeout(V.pollTimer); clearTimeout(V.resumeTimer);
    ui.video.pause(); ui.video.removeAttribute("src"); ui.video.load();
    Object.assign(V, { id: null, title: "", folder: "", segments: [], status: null, progress: 0, duration: 0 });
    ui.lines.innerHTML = ""; ui.pick.value = "";
    ui.box.classList.add("hidden"); ui.close.classList.add("hidden");
    setStatus("Pick a video and ask questions about it as you watch.");
    if (had) V.listeners.forEach((f) => f());
  }

  async function poll() {
    const id = V.id;
    if (id === null) return;
    try {
      const [item, segs] = await Promise.all([
        fetch(`/api/items/${id}`).then((r) => r.json()),
        fetch(`/api/items/${id}/segments?since=${V.segments.length}`).then((r) => r.json()),
      ]);
      if (id !== V.id) return;
      V.status = item.status; V.progress = item.progress || 0; V.duration = item.duration || V.duration;
      if (segs.length) { V.segments.push(...segs); renderLines(segs); }
      if (item.status === "ready") {
        setStatus(`Ready: the bot knows this video (${fmt(V.duration)}). Ask anything as you watch.`);
        return;
      }
      if (item.status === "error") { setStatus("Transcription failed: " + item.error + " (Retry it in Library)"); return; }
      setStatus(item.status === "transcribing" ? `Transcribing… ${Math.round(V.progress * 100)}% (you can start watching)`
        : item.status === "downloading" ? "Still downloading…" : "Waiting to be transcribed…");
    } catch { setStatus("Lost the server, retrying…"); }
    V.pollTimer = setTimeout(poll, 2000);
  }

  // ---------- transcript list ----------
  function renderLines(segs) {
    for (const s of segs) {
      const row = document.createElement("div");
      row.innerHTML = `<time>${fmt(s.start)}</time><span></span>`;
      row.lastChild.textContent = s.text;
      row.onclick = () => { ui.video.currentTime = s.start; };
      ui.lines.append(row);
    }
  }
  let lastNow = null;
  ui.video.addEventListener("timeupdate", () => {
    const t = ui.video.currentTime;
    if (playing()) V.lastPlayingAt = Date.now();
    let idx = -1;
    for (let i = 0; i < V.segments.length; i++) { if (V.segments[i].start <= t) idx = i; else break; }
    const row = ui.lines.children[idx];
    if (row !== lastNow) {
      lastNow?.classList.remove("now"); row?.classList.add("now"); lastNow = row;
      if (row && $("videoTranscript").open) row.scrollIntoView({ block: "nearest" });
    }
  });

  // ---------- link with Talk mode ----------
  ui.video.addEventListener("play", () => {
    clearTimeout(V.resumeTimer); V.pausedByBot = false;
    V.lastPlayingAt = Date.now();
    if (window.talk && !window.talk.isOn()) window.talk.start();   // the bot starts listening with the video
  });
  ui.video.addEventListener("pause", () => { V.pausedAt = Date.now(); });

  function wordsOf(t) { return (t.toLowerCase().match(/[a-z0-9']+/g) || []); }

  window.videoCtx = {
    openItem, close: closeVideo,
    active: () => V.id !== null,
    itemId: () => V.id,
    folder: () => V.folder,
    time: () => (V.id !== null ? ui.video.currentTime : null),
    onChange(f) { V.listeners.push(f); },
    seek(t) { if (V.id !== null) { ui.video.currentTime = t; } },
    // Speakers: the video's own sound leaks into the mic, so the gate is raised while it plays (you speak up a bit).
    onSpeakers: () => V.id !== null && playing() && ui.micMode.value === "paused",
    isEcho(text) {
      if (!V.segments.length || Date.now() - V.lastPlayingAt > 2500) return false;
      const t = ui.video.currentTime;
      const recent = new Set(wordsOf(V.segments.filter((s) => s.end >= t - 25 && s.start <= t + 3).map((s) => s.text).join(" ")));
      const w = wordsOf(text);
      if (w.length < 3 || !recent.size) return false;
      return w.filter((x) => recent.has(x)).length / w.length >= 0.6;
    },
    pauseForQuestion() {
      clearTimeout(V.resumeTimer);
      if (V.id === null) return;
      if (playing()) { V.pausedByBot = true; ui.video.pause(); }
      else if (Date.now() - V.pausedAt < 20000) V.pausedByBot = true;   // you paused it to ask
    },
    onUserSpeech() {                       // you started talking: pause right away, don't wait for the transcript
      clearTimeout(V.resumeTimer);
      if (V.id !== null && playing()) { V.pausedByBot = true; ui.video.pause(); }
    },
    onSpeechDiscarded() { this.onAnswerDone(); },   // it was only a cough or noise: carry on playing
    onAnswerDone() {
      clearTimeout(V.resumeTimer);
      if (V.id === null || !V.pausedByBot || !ui.autoResume.checked) return;
      V.resumeTimer = setTimeout(() => { if (V.pausedByBot && ui.video.paused) ui.video.play(); }, 3000);
    },
    context() {
      if (V.id === null) return "";
      const t = ui.video.currentTime;
      const head = `\n\nThe user is watching a video called "${V.title}" to learn, and pauses it to ask you questions. ` +
        `They are at ${fmt(t)}${V.duration ? ` of ${fmt(V.duration)}` : ""}.`;
      if (!V.segments.length) return head + " The transcript isn't ready yet.";
      const watched = V.segments.filter((s) => s.start <= t + 1);
      let text = watched.map((s) => `[${fmt(s.start)}] ${s.text}`).join("\n");
      if (text.length > 20000) text = "…" + text.slice(-20000);
      const recent = watched.filter((s) => s.end >= t - 45).map((s) => s.text).join(" ");
      return head +
        `\n\nTranscript of what they've watched so far:\n${text || "(nothing yet)"}` +
        `\n\nWhat was said in the last 45 seconds: ${recent || "(nothing)"}` +
        (V.status !== "ready" ? "\n(Later parts of the video are still being transcribed.)" : "") +
        `\n\nWhen they say "this", "that" or "he just said", they most likely mean the last 45 seconds. ` +
        `Explain clearly for a learner, and you can mention timestamps to rewatch.`;
    },
  };
  fillPicker();
})();
