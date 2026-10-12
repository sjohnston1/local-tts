// Talk mode: hands-free voice conversation.
// Mic -> voice activity detection -> Whisper (server) -> Ollama (streamed) -> Kokoro, sentence by sentence.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    orb: $("orb"), state: $("talkState"), level: $("level"), mark: $("thresholdMark"),
    toggle: $("talkToggle"), mute: $("muteMic"), newChat: $("newChat"), transcript: $("transcript"), empty: $("emptyNote"),
    model: $("llmModel"), sens: $("sens"), sensVal: $("sensVal"), bargeIn: $("bargeIn"),
    endSilence: $("endSilence"), system: $("systemPrompt"), voice: $("voice"), speed: $("speed"),
    askText: $("askText"), askSend: $("askSend"),
    scope: $("scope"), histBox: $("histBox"), histList: $("histList"), histSearch: $("histSearch"), histCount: $("histCount"),
  };
  const video = () => window.videoCtx;
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  const DEFAULT_SYSTEM =
    "You are a kind and helpful voice assistant having a spoken conversation. Reply naturally and briefly, " +
    "usually one to three sentences, unless asked for more detail. Your words are read aloud, so don't use " +
    "markdown, lists, headings or emojis. When showing code really helps, put a short example in a ``` code block: " +
    "it's shown on screen but not read aloud, so also say in words what it does.";

  const S = {
    on: false, phase: "off", muted: false,
    history: [],                  // [{role, content}]
    turn: 0, abort: null,
    current: null,                // {text, el} assistant reply in progress
    queue: [], pumping: false, streamDone: true, audio: new Audio(),
    // mic + VAD
    stream: null, ctx: null, node: null, sr: 48000,
    inSpeech: false, aboveMs: 0, silenceMs: 0, voicedMs: 0, utterMs: 0,
    buf: [], pre: [], preMs: 0,
  };

  // ---------- settings ----------
  ui.system.value = store.get("talk.system", DEFAULT_SYSTEM);
  $("resetPrompt").onclick = () => {
    ui.system.value = DEFAULT_SYSTEM;
    store.set("talk.system", DEFAULT_SYSTEM);
    snack("Personality reset to the default.");
  };
  ui.system.onchange =() => store.set("talk.system", ui.system.value.trim() || DEFAULT_SYSTEM);
  ui.sens.value = store.get("talk.gate", "-45");              // noise gate, in dB: quieter than this is ignored
  ui.bargeIn.checked = store.get("talk.bargeIn", "1") === "1";
  ui.endSilence.value = store.get("talk.endSilence", "800");
  // Level meter and gate share one scale: -80 dB (silence) to -10 dB (very loud).
  const dbOf = (v) => 20 * Math.log10(Math.max(v, 1e-5));
  const toPct = (v) => Math.max(0, Math.min(1, (dbOf(v) + 80) / 70)) * 100;
  const gate = () => Math.pow(10, Number(ui.sens.value) / 20);
  const showSens = () => (ui.sensVal.textContent = `${ui.sens.value} dB`);
  showSens();
  ui.sens.oninput = () => { showSens(); store.set("talk.gate", ui.sens.value); };
  ui.libMode = $("libMode");
  ui.libMode.value = store.get("talk.libMode", "only");
  ui.libMode.onchange = () => store.set("talk.libMode", ui.libMode.value);
  ui.showSources = $("showSources");
  ui.showSources.checked = store.get("talk.showSources", "1") === "1";
  ui.showSources.onchange = () => store.set("talk.showSources", ui.showSources.checked ? "1" : "0");
  ui.bargeIn.onchange =() => store.set("talk.bargeIn", ui.bargeIn.checked ? "1" : "0");
  ui.endSilence.onchange = () => store.set("talk.endSilence", ui.endSilence.value);
  ui.model.onchange = () => { store.set("talk.model", ui.model.value); warmModel(); };

  // Load the model into memory now so the first question isn't slow.
  let warming = 0;
  async function warmModel() {
    const name = ui.model.value;
    if (!name) return;
    const id = ++warming;
    const label = ui.state.textContent;
    ui.state.textContent = "Loading model…";
    try {
      const r = await fetch("/api/llm-warm", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: name }) });
      if (!r.ok) throw new Error((await r.json()).detail);
    } catch (e) { snack("Couldn't load model: " + e.message); }
    if (id === warming && ui.state.textContent === "Loading model…") ui.state.textContent = label;
  }

  async function loadModels() {
    try {
      const res = await fetch("/api/llm-models");
      if (!res.ok) throw new Error((await res.json()).detail);
      const names = await res.json();
      ui.model.innerHTML = "";
      if (!names.length) { ui.model.append(new Option("No models in Ollama yet", "")); return; }
      for (const n of names) ui.model.append(new Option(n, n));
      const saved = store.get("talk.model", "");
      if (names.includes(saved)) ui.model.value = saved;
      warmModel();
    } catch (e) {
      ui.model.innerHTML = "";
      ui.model.append(new Option("Ollama not running", ""));
    }
  }

  // ---------- UI helpers ----------
  const LABELS = {
    off: "Tap the mic to start", listening: "Listening…", hearing: "Hearing you…",
    transcribing: "Got it…", thinking: "Thinking…", speaking: "Speaking (talk to interrupt)",
  };
  function setPhase(p) {
    S.phase = p;
    ui.state.textContent = S.muted && S.on ? "Mic muted" + (p === "speaking" ? " · Speaking…" : "")
      : p === "speaking" && !ui.bargeIn.checked ? "Speaking…" : LABELS[p];
    ui.orb.className = { off: "", listening: "listening", hearing: "hearing", transcribing: "busy",
                         thinking: "busy", speaking: "speaking" }[p];
  }
  function snack(msg) {
    const s = $("snack"); s.textContent = msg; s.classList.add("show");
    clearTimeout(snack.t); snack.t = setTimeout(() => s.classList.remove("show"), 5000);
  }
  function bubble(role, text) {
    ui.empty?.remove();
    const el = document.createElement("div");
    el.className = `msg ${role}`; el.textContent = text;
    ui.transcript.append(el);
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
    return el;
  }

  // ---------- mic capture + voice activity detection ----------
  const WORKLET = `
    class Cap extends AudioWorkletProcessor {
      constructor() { super(); this.b = new Float32Array(1024); this.n = 0; }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) for (let i = 0; i < ch.length; i++) {
          this.b[this.n++] = ch[i];
          if (this.n === this.b.length) { this.port.postMessage(this.b.slice(0)); this.n = 0; }
        }
        return true;
      }
    }
    registerProcessor("cap", Cap);`;

  async function startMic() {
    S.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    S.ctx = new AudioContext();
    S.sr = S.ctx.sampleRate;
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
    await S.ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    const src = S.ctx.createMediaStreamSource(S.stream);
    S.node = new AudioWorkletNode(S.ctx, "cap");
    const mute = S.ctx.createGain(); mute.gain.value = 0;
    src.connect(S.node); S.node.connect(mute); mute.connect(S.ctx.destination);
    S.node.port.onmessage = (e) => onFrame(e.data);
    S.inSpeech = false; S.aboveMs = 0; S.pre = []; S.preMs = 0;
  }
  function stopMic() {
    S.node?.port && (S.node.port.onmessage = null);
    S.stream?.getTracks().forEach((t) => t.stop());
    S.ctx?.close().catch(() => {});
    S.stream = S.ctx = S.node = null;
    ui.level.style.width = "0";
  }

  // The gate is a fixed level you set; while the bot is talking it's 6 dB higher so its own voice doesn't trigger it.
  // While a video plays through speakers it's raised another ~10 dB, so only clearly louder speech counts.
  const threshold = (bargeIn) => gate() * (bargeIn ? 2 : 1) * (video()?.onSpeakers() ? 3 : 1);
  const busy = () => ["transcribing", "thinking", "speaking"].includes(S.phase);

  function onFrame(frame) {
    if (!S.on) return;
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const rms = Math.sqrt(sum / frame.length);
    const ms = (frame.length / S.sr) * 1000;

    if (S.muted) { ui.level.style.width = "0"; return; }          // muted: ignore the mic entirely

    const bargeMode = busy();
    if (bargeMode && !ui.bargeIn.checked) { ui.level.style.width = toPct(rms) + "%"; return; }
    const thr = threshold(bargeMode);
    ui.level.style.width = toPct(rms) + "%";
    ui.mark.style.left = toPct(thr) + "%";                        // dashed line: the higher gate used while the bot talks
    ui.mark.style.opacity = bargeMode ? 1 : 0;

    if (!S.inSpeech) {
      S.pre.push(frame); S.preMs += ms;                           // keep ~300 ms before speech starts
      while (S.preMs > 300) S.preMs -= (S.pre.shift().length / S.sr) * 1000;
      S.aboveMs = rms > thr ? S.aboveMs + ms : 0;
      if (S.aboveMs >= (bargeMode ? 250 : 90)) {
        if (bargeMode) interrupt();
        S.inSpeech = true; S.buf = S.pre.slice(); S.pre = []; S.preMs = 0;
        S.silenceMs = 0; S.voicedMs = S.aboveMs; S.utterMs = 0;
        video()?.onUserSpeech();
        setPhase("hearing");
      }
    } else {
      S.buf.push(frame); S.utterMs += ms;
      if (rms > thr * 0.7) { S.silenceMs = 0; S.voicedMs += ms; } else S.silenceMs += ms;
      if (S.silenceMs >= Number(ui.endSilence.value) || S.utterMs > 30000) endUtterance();
    }
  }

  function endUtterance() {
    S.inSpeech = false; S.aboveMs = 0;
    const frames = S.buf; S.buf = [];
    if (S.voicedMs < 140) { setPhase("listening"); video()?.onSpeechDiscarded(); return; }     // just a click or cough
    let len = 0; for (const f of frames) len += f.length;
    const all = new Float32Array(len);
    let o = 0; for (const f of frames) { all.set(f, o); o += f.length; }
    handleUtterance(downsample(all, S.sr, 16000));
  }
  function downsample(input, from, to) {
    if (from === to) return input;
    const ratio = from / to, out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const a = Math.floor(i * ratio), b = Math.min(input.length, Math.floor((i + 1) * ratio));
      let s = 0; for (let j = a; j < b; j++) s += input[j];
      out[i] = s / Math.max(1, b - a);
    }
    return out;
  }

  // ---------- one conversation turn ----------
  const JUNK = /^(thanks? (you )?for watching[.!]*|you|\.+|subtitles by.*)$/i;   // common Whisper noise

  async function handleUtterance(pcm) {
    const turn = ++S.turn;
    const ctrl = new AbortController(); S.abort = ctrl;
    setPhase("transcribing");
    let text;
    try {
      const r = await fetch("/api/transcribe", { method: "POST", body: pcm.buffer, signal: ctrl.signal,
        headers: { "Content-Type": "application/octet-stream" } });
      if (!r.ok) throw new Error((await r.json()).detail);
      text = (await r.json()).text.trim();
    } catch (e) {
      if (turn === S.turn && e.name !== "AbortError") { snack(e.message); setPhase("listening"); }
      return;
    }
    if (turn !== S.turn) return;
    if (!text || JUNK.test(text) || video()?.isEcho(text)) { setPhase("listening"); video()?.onSpeechDiscarded(); return; }
    await ask(text, turn, ctrl);
  }

  // A question from the mic or the text box.
  async function ask(text, turn, ctrl) {
    if (turn === undefined) {                      // typed question
      if (busy()) interrupt();
      turn = ++S.turn; ctrl = new AbortController(); S.abort = ctrl;
    }
    video()?.pauseForQuestion();
    bubble("user", text);
    S.history.push({ role: "user", content: text });
    saveMessage("user", text);
    if (!ui.model.value) { snack("Pick an AI model first (is Ollama running?)"); idle(); return; }
    await reply(turn, ctrl);
  }
  function idle() { setPhase(S.on ? "listening" : "off"); }

  async function reply(turn, ctrl) {
    setPhase("thinking");
    S.current = { text: "", el: null };
    S.streamDone = false;
    let raw = "", spoken = 0;
    const system = (ui.system.value.trim() || DEFAULT_SYSTEM) + (video()?.context() || "");
    const messages = [{ role: "system", content: system }, ...S.history];
    try {
      const r = await fetch("/api/chat", { method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: ui.model.value, messages, folder: S.doc ? null : scopeFolder(),
                               video_id: video()?.itemId() ?? null, video_time: video()?.time() ?? null,
                               library_mode: ui.libMode.value,
                               doc_item_id: S.doc?.itemId ?? null, doc_title: S.doc?.title ?? null,
                               doc_text: S.doc?.text ?? null }) });
      if (!r.ok) throw new Error((await r.json()).detail);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let pending = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        pending += dec.decode(value, { stream: true });
        const lines = pending.split("\n"); pending = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          const j = JSON.parse(line);
          if (j.error) throw new Error(j.error);
          if (j.sources) { S.current.sources = j.sources; continue; }
          raw += j.message?.content || "";
        }
        if (turn !== S.turn) return;
        const visible = stripThink(raw);
        S.current.text = visible;
        if (visible.trim()) {
          S.current.el ??= bubble("assistant", "");
          S.current.el.textContent = visible;
          ui.transcript.scrollTop = ui.transcript.scrollHeight;
        }
        // speak every complete sentence as soon as it arrives
        let end;
        while ((end = nextSentenceEnd(visible.slice(spoken), spoken === 0 ? 12 : 40)) > 0) {
          enqueueSpeech(visible.slice(spoken, spoken + end), turn);
          spoken += end;
        }
      }
    } catch (e) {
      if (turn === S.turn && e.name !== "AbortError") { snack(e.message); S.current = null; S.streamDone = true; idle(); }
      return;
    }
    if (turn !== S.turn) return;
    const visible = stripThink(raw);
    if (visible.slice(spoken).trim()) enqueueSpeech(visible.slice(spoken), turn);
    if (visible.trim()) {
      S.history.push({ role: "assistant", content: visible.trim() });
      saveMessage("assistant", visible.trim());
      if (S.current.el && S.current.sources?.length && ui.showSources.checked) showSources(S.current.el, S.current.sources);
    }
    S.current = null;
    S.streamDone = true;
    S.answered = true;
    if (!S.queue.length && !S.pumping) answerFinished();
  }
  function answerFinished() {
    idle();
    if (S.answered) { S.answered = false; video()?.onAnswerDone(); }
  }

  function stripThink(t) {
    t = t.replace(/<think>[\s\S]*?<\/think>/g, "");
    const open = t.indexOf("<think>");
    return (open >= 0 ? t.slice(0, open) : t).replace(/^\s+/, "");
  }
  // Index just past the first sentence boundary that leaves at least minLen chars, or -1.
  function nextSentenceEnd(text, minLen) {
    const re = /[.!?…]+["')\]]*\s+|\n+/g;
    let m;
    while ((m = re.exec(text))) {
      const end = m.index + m[0].length;
      if (text.slice(0, end).trim().length >= minLen) return end;
    }
    return -1;
  }
  function cleanForSpeech(t) {
    return t.replace(/```[\s\S]*?```/g, " ")
      .replace(/https?:\/\/\S+/g, "a link")
      .replace(/[*_#`>~|]+/g, "")
      .replace(/^\s*[-•]\s+/gm, "")
      .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, "")
      .replace(/\s+/g, " ").trim();
  }

  // ---------- speech output queue ----------
  function enqueueSpeech(text, turn) {
    const clean = cleanForSpeech(text);
    if (!/[a-z0-9]/i.test(clean)) return;
    const p = fetch("/api/speak", { method: "POST", signal: S.abort?.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: clean, voice: ui.voice.value || "af_heart", speed: Number(ui.speed.value) || 1 }),
    }).then((r) => { if (!r.ok) throw new Error("Voice generation failed"); return r.blob(); })
      .then((b) => URL.createObjectURL(b));
    p.catch(() => {});
    S.queue.push({ p, turn });
    if (!S.pumping) pump();
  }
  async function pump() {
    S.pumping = true;
    while (S.queue.length) {
      const { p, turn } = S.queue.shift();
      let url;
      try { url = await p; } catch (e) { if (turn === S.turn && e.name !== "AbortError") snack(e.message); continue; }
      if (turn !== S.turn) { URL.revokeObjectURL(url); continue; }
      setPhase("speaking");
      await new Promise((resolve) => {
        S.audio.onended = S.audio.onerror = S.audio.onpause = resolve;
        S.audio.src = url;
        S.audio.play().catch(resolve);
      });
      URL.revokeObjectURL(url);
    }
    S.pumping = false;
    if (S.streamDone && !S.inSpeech && S.phase !== "transcribing" && S.phase !== "thinking") answerFinished();
  }

  // Stop whatever the assistant is doing (you started talking, or you tapped the orb).
  function interrupt() {
    S.turn++;
    S.answered = false;
    S.abort?.abort(); S.abort = null;
    S.queue = [];
    S.audio.pause();
    if (S.current) {
      if (S.current.text.trim()) {
        S.history.push({ role: "assistant", content: S.current.text.trim() });
        saveMessage("assistant", S.current.text.trim(), true);
      }
      S.current.el?.classList.add("interrupted");
      S.current = null;
    }
    S.streamDone = true;
  }

  // ---------- start / stop ----------
  async function start() {
    if (!navigator.mediaDevices?.getUserMedia) { snack("This browser can't use the microphone here."); return; }
    try { await startMic(); }
    catch (e) { snack("Microphone blocked: " + e.message); stopMic(); return; }
    S.on = true;
    ui.mute.disabled = false;
    ui.toggle.textContent = "Stop conversation";
    setPhase("listening");
    if (!ui.model.value) loadModels();
  }
  function stop() {
    if (!S.on && S.phase === "off") return;
    interrupt();
    S.on = false; S.inSpeech = false;
    stopMic();
    S.muted = false; ui.mute.textContent = "Mute mic"; ui.mute.classList.remove("filled"); ui.mute.disabled = true;
    ui.toggle.textContent = "Start conversation";
    setPhase("off");
  }
  function setMuted(m) {
    S.muted = m;
    if (m) {                                                     // drop anything half-heard
      S.inSpeech = false; S.buf = []; S.pre = []; S.preMs = 0; S.aboveMs = 0;
      if (S.phase === "hearing") { S.phase = "listening"; video()?.onSpeechDiscarded(); }
    }
    ui.mute.textContent = m ? "Unmute mic" : "Mute mic";
    ui.mute.classList.toggle("filled", m);
    setPhase(S.phase);
  }
  ui.mute.onclick = () => setMuted(!S.muted);
  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() !== "m" || e.ctrlKey || e.metaKey || e.altKey || !S.on) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    setMuted(!S.muted);
  });
  ui.toggle.onclick = () => (S.on ? stop() : start());
  ui.orb.onclick = () => {
    if (!S.on) return start();
    if (busy()) { interrupt(); setPhase("listening"); }   // tap to cut it off
  };
  function clearTranscript(note) {
    ui.transcript.innerHTML = "";
    const p = document.createElement("p");
    p.className = "muted empty"; p.textContent = note;
    ui.empty = p; ui.transcript.append(p);
  }
  ui.newChat.onclick = () => {
    interrupt();
    S.history = []; S.convId = null; S.convPromise = null;
    clearTranscript("New conversation. It's saved automatically once you ask something.");
    renderHistory();
    if (S.on) setPhase("listening");
  };

  // ---------- ask about one document (library file, web page or pasted text) ----------
  function showDoc() {
    $("docChip").classList.toggle("hidden", !S.doc);
    if (S.doc) $("docName").textContent = S.doc.title;
  }
  // doc: { title, itemId } for a library file, or { title, text } for text from the Read tab.
  function focusDoc(doc) {
    ui.newChat.onclick();                         // fresh conversation about this document
    S.doc = { title: doc.title, itemId: doc.itemId ?? null, text: doc.text ? doc.text.slice(0, 1_500_000) : null };
    showDoc();
    clearTranscript(`Ask anything about "${S.doc.title}". Tap Start conversation to talk, or type below.`);
    ui.askText.focus();
    if (!ui.model.value) loadModels();
  }
  $("docClear").onclick = () => { S.doc = null; showDoc(); };

  // ---------- library search scope ----------
  function scopeFolder() {
    const v = ui.scope.value;
    return v === "__off" ? null : v;            // "" = whole library
  }
  function fillScope() {
    const keep = ui.scope.value || store.get("talk.scope", "");
    ui.scope.innerHTML = "";
    ui.scope.append(new Option("Whole library", ""), new Option("Don't search the library", "__off"));
    for (const f of window.lib?.folders || []) ui.scope.append(new Option("Folder: " + f.split("/").join(" / "), f));
    ui.scope.value = [...ui.scope.options].some((o) => o.value === keep) ? keep : "";
  }
  ui.scope.onchange = () => store.set("talk.scope", ui.scope.value);
  window.lib?.onChange(fillScope);
  // Watching a video? Look things up in that video's folder by default.
  window.videoCtx?.onChange(() => {
    const f = video()?.folder();
    if (video()?.active() && f && [...ui.scope.options].some((o) => o.value === f)) { ui.scope.value = f; }
  });

  function showSources(el, sources) {
    const seen = new Set(), wrap = document.createElement("div");
    wrap.className = "sources"; wrap.append("From your library: ");
    for (const s of sources) {
      const key = s.title + "|" + (s.loc || "");
      if (seen.has(key) || seen.size >= 4) continue;
      if (seen.size) wrap.append(", ");
      seen.add(key);
      const a = document.createElement("a");
      a.textContent = s.title + (s.loc ? ` (${s.loc})` : "");
      a.onclick = () => {
        if (s.kind === "video") video()?.openItem(s.item_id, s.start);
        else fetch(`/api/items/${s.item_id}/text`).then((r) => r.json()).then((t) => window.readAloud(t.text, s.title));
      };
      wrap.append(a);
    }
    el.after(wrap);
  }

  // ---------- saved conversations (SQLite) ----------
  async function ensureConversation() {
    if (S.convId) return S.convId;
    S.convPromise ??= fetch("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: scopeFolder(), item_id: video()?.itemId() ?? null }) })
      .then((r) => r.json()).then((d) => { S.convId = d.id; return d.id; });
    return S.convPromise;
  }
  async function saveMessage(role, content, interrupted = false) {
    const videoTime = video()?.time() ?? null;
    try {
      const id = await ensureConversation();
      await fetch(`/api/conversations/${id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, content, video_time: videoTime, interrupted }) });
      if (role === "user") loadHistory(id);
    } catch (e) { console.warn("Couldn't save message", e); }
  }
  // ---------- past conversations list ----------
  const ICON_EDIT = '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
  const ICON_DEL = '<svg viewBox="0 0 24 24"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
  let convs = [];

  async function loadHistory() {
    try { convs = await (await fetch("/api/conversations")).json(); } catch { return; }
    renderHistory();
  }
  function dayGroup(ts) {
    const d = new Date(ts * 1000), today = new Date(); today.setHours(0, 0, 0, 0);
    const days = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return "Previous 7 days";
    if (days < 30) return "Previous 30 days";
    return d.toLocaleDateString([], { month: "long", year: "numeric" });
  }
  function renderHistory() {
    const q = ui.histSearch.value.trim().toLowerCase();
    const shown = convs.filter((c) => !q || c.title.toLowerCase().includes(q));
    ui.histCount.textContent = convs.length ? `(${convs.length})` : "";
    ui.histList.innerHTML = "";
    if (!shown.length) {
      const p = document.createElement("p");
      p.className = "muted"; p.style.padding = "4px 10px";
      p.textContent = convs.length ? "No conversations match." : "No saved conversations yet.";
      ui.histList.append(p);
      return;
    }
    let group = null;
    for (const c of shown) {
      const g = dayGroup(c.updated_at);
      if (g !== group) {
        group = g;
        const h = document.createElement("div"); h.className = "hgroup"; h.textContent = g;
        ui.histList.append(h);
      }
      ui.histList.append(historyRow(c));
    }
  }
  function historyRow(c) {
    const row = document.createElement("div");
    row.className = "hi" + (c.id === S.convId ? " sel" : "");
    const time = new Date(c.updated_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const info = document.createElement("div");
    info.style.minWidth = "0";
    info.innerHTML = '<div class="t"></div><div class="m"></div>';
    info.firstChild.textContent = c.title;
    info.lastChild.textContent = `${time} · ${c.count} message${c.count === 1 ? "" : "s"}`;
    const acts = document.createElement("div"); acts.className = "a";
    const edit = document.createElement("button"); edit.title = "Rename"; edit.innerHTML = ICON_EDIT;
    const del = document.createElement("button"); del.title = "Delete"; del.innerHTML = ICON_DEL;
    acts.append(edit, del);
    row.append(info, acts);
    row.onclick = () => openConversation(c.id);
    edit.onclick = (e) => { e.stopPropagation(); startRename(row, info, acts, c); };
    del.onclick = (e) => { e.stopPropagation(); deleteConversation(c); };
    return row;
  }
  function startRename(row, info, acts, c) {
    const input = document.createElement("input");
    input.value = c.title; input.maxLength = 200;
    row.onclick = null;
    info.replaceChildren(input); acts.remove();
    input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const title = input.value.trim();
      if (save && title && title !== c.title) {
        try {
          const r = await fetch(`/api/conversations/${c.id}`, { method: "PATCH",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) });
          if (!r.ok) throw new Error("Rename failed");
          c.title = title;
        } catch (e) { snack(e.message); }
      }
      renderHistory();
    };
    input.onkeydown = (e) => { if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
    input.onblur = () => finish(true);
    input.onclick = (e) => e.stopPropagation();
  }
  async function deleteConversation(c) {
    if (!confirm(`Delete "${c.title}"?`)) return;
    await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
    if (S.convId === c.id) ui.newChat.onclick();
    loadHistory();
  }
  async function openConversation(id) {
    interrupt();
    const c = await (await fetch(`/api/conversations/${id}`)).json();
    S.convId = id; S.convPromise = Promise.resolve(id);
    S.history = c.messages.map((m) => ({ role: m.role, content: m.content }));
    clearTranscript("");
    ui.empty.remove();
    for (const m of c.messages) {
      const el = bubble(m.role, m.content);
      if (m.interrupted) el.classList.add("interrupted");
    }
    if (c.folder !== null && [...ui.scope.options].some((o) => o.value === c.folder)) ui.scope.value = c.folder;
    if (c.item_id) {                                   // reopen the video where you left off
      const last = [...c.messages].reverse().find((m) => m.video_time != null);
      video()?.openItem(c.item_id, last?.video_time ?? null, false);   // reopening a saved chat: don't start playing
    }
    renderHistory();
    if (S.on) setPhase("listening");
  }
  ui.histSearch.oninput = renderHistory;

  function sendTyped() {
    const t = ui.askText.value.trim();
    if (!t) return;
    ui.askText.value = "";
    ask(t);
  }
  ui.askSend.onclick = sendTyped;
  ui.askText.onkeydown = (e) => { if (e.key === "Enter") sendTyped(); };

  window.talk = { stop, start, isOn: () => S.on, focusDoc };
  loadModels();
  fillScope();
  loadHistory();
})();
