"""Local TTS server: Kokoro-82M voices, Whisper speech-to-text, an Ollama chat proxy and a
SQLite-backed library of videos and documents, all on your GPU, served with FastAPI.

Run:  python -m uvicorn app:app --port 48765   (from the server folder)
Then open http://localhost:48765
"""
import io
import json
import os
import re
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import db
import library
import textutil

SAMPLE_RATE = 24000
REPO_ID = "hexgrad/Kokoro-82M"

# Kokoro English voices. First letter = accent (a = American, b = British), second = gender.
VOICES = {
    "af_heart": "Heart", "af_bella": "Bella", "af_nicole": "Nicole", "af_aoede": "Aoede",
    "af_kore": "Kore", "af_sarah": "Sarah", "af_nova": "Nova", "af_sky": "Sky",
    "af_alloy": "Alloy", "af_jessica": "Jessica", "af_river": "River",
    "am_michael": "Michael", "am_fenrir": "Fenrir", "am_puck": "Puck", "am_echo": "Echo",
    "am_eric": "Eric", "am_liam": "Liam", "am_onyx": "Onyx", "am_adam": "Adam",
    "am_santa": "Santa",
    "bf_emma": "Emma", "bf_isabella": "Isabella", "bf_alice": "Alice", "bf_lily": "Lily",
    "bm_george": "George", "bm_fable": "Fable", "bm_lewis": "Lewis", "bm_daniel": "Daniel",
}

# Speech-to-text model for Talk mode (about 1.6 GB, downloaded on first run).
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "openai/whisper-large-v3-turbo")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# Context window for chat. Video transcripts need room; lower it if you run short on VRAM.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "english")

_lock = threading.Lock()          # one TTS job at a time
_stt_lock = threading.Lock()      # one transcription at a time
_pipelines: dict = {}
_model = None
_device = "not loaded"
_asr = None


def get_pipeline(voice: str):
    """Load Kokoro lazily; American and British pipelines share one model on the GPU."""
    global _model, _device
    lang = voice[0]
    if lang in _pipelines:
        return _pipelines[lang]
    import torch
    from kokoro import KModel, KPipeline
    if _model is None:
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        _model = KModel(repo_id=REPO_ID).to(_device).eval()
        if _device == "cuda":
            _device = f"cuda ({torch.cuda.get_device_name(0)})"
    _pipelines[lang] = KPipeline(lang_code=lang, repo_id=REPO_ID, model=_model)
    return _pipelines[lang]


def get_asr():
    """Load Whisper lazily (fp16 on the GPU)."""
    global _asr
    if _asr is None:
        import torch
        from transformers import pipeline
        cuda = torch.cuda.is_available()
        _asr = pipeline(
            "automatic-speech-recognition", model=WHISPER_MODEL,
            torch_dtype=torch.float16 if cuda else torch.float32,
            device="cuda:0" if cuda else "cpu",
        )
    return _asr


app = FastAPI(title="Local TTS")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str = "af_heart"
    speed: float = Field(1.0, ge=0.5, le=2.0)


class TextRequest(BaseModel):
    text: str


class UrlRequest(BaseModel):
    url: str


@app.get("/api/health")
def health():
    return {"ok": True, "device": _device, "stt": _asr is not None}


@app.get("/api/voices")
def voices():
    out = []
    for vid, name in VOICES.items():
        out.append({
            "id": vid, "name": name,
            "accent": "American" if vid[0] == "a" else "British",
            "gender": "Female" if vid[1] == "f" else "Male",
        })
    return out


@app.post("/api/chunks")
def chunks(req: TextRequest):
    return {"chunks": textutil.split_chunks_with_paras(req.text)}


@app.post("/api/speak")
def speak(req: SpeakRequest):
    import numpy as np
    import soundfile as sf
    if req.voice not in VOICES:
        raise HTTPException(400, f"Unknown voice {req.voice}")
    with _lock:
        pipeline = get_pipeline(req.voice)
        parts = []
        for result in pipeline(req.text, voice=req.voice, speed=req.speed, split_pattern=None):
            audio = result.audio if hasattr(result, "audio") else result[2]
            if audio is not None:
                parts.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
    if not parts:
        raise HTTPException(422, "Nothing to speak in that text")
    buf = io.BytesIO()
    sf.write(buf, np.concatenate(parts), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return Response(buf.getvalue(), media_type="audio/wav")


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    try:
        text = textutil.extract_file(file.filename or "file.txt", data)
    except Exception as e:  # bad/encrypted PDF etc.
        raise HTTPException(422, f"Couldn't read {file.filename}: {e}")
    text = textutil.clean_text(text)
    if not text:
        raise HTTPException(422, "No readable text found (scanned PDFs need OCR first).")
    return {"title": file.filename, "text": text}


@app.post("/api/extract-url")
def extract_url(req: UrlRequest):
    try:
        title, text = textutil.fetch_url(req.url.strip())
    except Exception as e:
        raise HTTPException(422, f"Couldn't load that page: {e}")
    text = textutil.clean_text(text)
    if not text:
        raise HTTPException(422, "No readable text found on that page.")
    return {"title": title or req.url, "text": text}


# ---------- Talk mode: speech-to-text + Ollama ----------

@app.post("/api/transcribe")
async def transcribe(request: Request):
    """Body: raw little-endian float32 mono PCM at 16 kHz. Returns {"text": ...}."""
    import numpy as np
    data = await request.body()
    audio = np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4").astype(np.float32)
    if audio.size < 16000 * 0.2:      # under 0.2 s: nothing worth transcribing
        return {"text": ""}
    if audio.size < 16000 * 1.0:      # very short ("why?", "what?"): pad with silence, Whisper reads those more reliably
        audio = np.concatenate([audio, np.zeros(16000 - audio.size, dtype=np.float32)])

    def run():
        with _stt_lock:
            out = get_asr()({"raw": audio, "sampling_rate": 16000},
                            generate_kwargs={"language": WHISPER_LANGUAGE, "task": "transcribe"})
        return (out.get("text") or "").strip()

    try:
        return {"text": await run_in_threadpool(run)}
    except Exception as e:
        raise HTTPException(500, f"Speech-to-text failed: {e}")


@app.get("/api/llm-models")
def llm_models():
    import requests
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
    except Exception:
        raise HTTPException(503, f"Can't reach Ollama at {OLLAMA_URL} - is it running?")
    # Hide things that aren't chat models (image-model text encoders, embedding models).
    not_chat = re.compile(r"text-encoder|flux|embed|rerank", re.I)
    return sorted(m["name"] for m in r.json().get("models", []) if not not_chat.search(m["name"]))


class WarmRequest(BaseModel):
    model: str


@app.post("/api/llm-warm")
def llm_warm(req: WarmRequest):
    """Load a model into memory now (same context size and keep-alive as chat, so it isn't reloaded later)."""
    import requests
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=(5, 600),
                          json={"model": req.model, "messages": [], "keep_alive": "30m",
                                "options": {"num_ctx": OLLAMA_NUM_CTX}})
    except Exception:
        raise HTTPException(503, f"Can't reach Ollama at {OLLAMA_URL} - is it running?")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Ollama: {r.text}")
    return {"ok": True}


# ---------- Talk mode: chat with your library ----------

class ChatRequest(BaseModel):
    model: str
    messages: list[dict]
    folder: str | None = None          # search scope: None = don't search, "" = whole library
    video_id: int | None = None        # video being watched (search won't jump ahead of it)
    video_time: float | None = None
    library_mode: str = "only"         # "only": answer from the library's documents; "prefer": fall back to general knowledge
    doc_item_id: int | None = None     # ask about one library file
    doc_title: str | None = None       # ask about text from the Read tab (web page, upload, pasted text)
    doc_text: str | None = Field(default=None, max_length=2_000_000)


def library_has_content() -> bool:
    with db.db() as c:
        return c.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None


DOC_FULL_CHARS = 24000                # documents up to this size are given to the model whole


def document_context(req: "ChatRequest") -> tuple[str, list[dict]]:
    """Context for questions about one specific document: the library file or the text from the Read tab."""
    users = [m.get("content", "") for m in req.messages if m.get("role") == "user"]
    query = " ".join(users[-2:]) if users else ""
    sources: list[dict] = []
    if req.doc_item_id is not None:
        hits = library.search(query, "", limit=8, item_id=req.doc_item_id)
        if len(hits) < 3:                                    # e.g. "summarize this": add the opening
            seen = {h["id"] for h in hits}
            hits = library.item_opening(req.doc_item_id, 3) + [h for h in hits if h["id"] not in seen]
        parts, total = [], 0
        for h in hits:
            text = h["text"][:2500]
            if total + len(text) > 12000:
                break
            total += len(text)
            parts.append(f"{'[' + h['loc'] + '] ' if h['loc'] else ''}{text}")
        if not parts:
            return "", []
        title = req.doc_title or hits[0]["title"]
        body = "\n\n".join(parts)
        sources = [{"item_id": hits[0]["item_id"], "title": hits[0]["title"], "loc": h["loc"],
                    "start": h["start"], "kind": h["kind"]} for h in hits[:1]]
        note = "These are the most relevant passages of it."
    else:
        text = (req.doc_text or "").strip()
        if not text:
            return "", []
        title = req.doc_title or "the document"
        if len(text) <= DOC_FULL_CHARS:
            body, note = text, "Here is its full text."
        else:                                                # long: opening plus the passages that match the question
            passages = library._split_passages(text, 1500)
            terms = {t for t in re.findall(r"[a-z0-9_+#]+", query.lower())
                     if t not in library.STOPWORDS and len(t) > 1}
            scored = sorted(range(1, len(passages)),
                            key=lambda i: -sum(passages[i].lower().count(t) for t in terms))
            keep = sorted({0, *scored[:7]})
            body = "\n\n[...]\n\n".join(passages[i] for i in keep)[:DOC_FULL_CHARS]
            note = "It is long, so these are its opening and the passages most relevant to the question."
    ctx = (f"\n\nThe user is asking about this document: \"{title}\". {note}\n\n{body}\n\n"
           "Answer from the document. If it doesn't cover the question, say so briefly rather than guessing.")
    return ctx, sources


def library_context(req: "ChatRequest") -> tuple[str, list[dict]]:
    """Search the library for the latest question and format the best passages for the model."""
    if req.doc_item_id is not None or req.doc_text:
        return document_context(req)
    if req.folder is None:
        return "", []
    users = [m.get("content", "") for m in req.messages if m.get("role") == "user"]
    if not users:
        return "", []
    hits = library.search(users[-1], req.folder, limit=8 if req.library_mode == "only" else 6,
                          video_id=req.video_id, video_time=req.video_time)
    if len(hits) < 3 and len(users) > 1:             # short follow-ups: also use the previous question
        seen = {h["id"] for h in hits}
        hits += [h for h in library.search(users[-1] + " " + users[-2], req.folder, limit=6,
                                           video_id=req.video_id, video_time=req.video_time)
                 if h["id"] not in seen][: 6 - len(hits)]
    # Drop weak matches: a passage must share at least two distinctive words with the question (one if it only has one),
    # otherwise unrelated passages get quoted as "sources" under every answer.
    generic = {"way", "set", "use", "using", "used", "allow", "able", "thing", "things", "something", "anything",
               "give", "find", "look", "going", "go", "new", "work", "works", "try", "help"}

    def terms_of(q: str) -> list[str]:
        return [t for t in dict.fromkeys(re.findall(r"[a-z0-9_+#]+", q.lower()))
                if t not in library.STOPWORDS and t not in generic and len(t) > 1]

    def has(tokens: set[str], t: str) -> bool:           # whole word, or a word starting with the stem (add -> added)
        if len(t) <= 4:
            return t in tokens
        stem = t[:-2]
        return any(w.startswith(stem) for w in tokens)

    own = terms_of(users[-1])
    pool = list(dict.fromkeys(own + (terms_of(users[-2]) if len(users) > 1 else [])))
    need = max(1, min(2 if req.library_mode == "only" else 3, -(-len(own) * 2 // 5)))   # ~40% of the question's words
    kept = []
    for h in hits:
        tokens = set(re.findall(r"[a-z0-9_+#]+", (h["title"] + " " + h["text"]).lower()))
        if sum(has(tokens, t) for t in pool) >= need:
            kept.append(h)
    hits = kept
    only = req.library_mode == "only"
    if not hits:
        if only and library_has_content():
            return ("\n\nThe user wants answers taken from their own uploaded documents. Nothing in their library "
                    "matched this question. Say briefly that you couldn't find it in their documents and suggest "
                    "rephrasing or naming the document. Do not answer from general knowledge unless they ask you to "
                    "(plain greetings and small talk are fine)."), []
        return "", []
    parts, total = [], 0
    for n, h in enumerate(hits, 1):
        text = h["text"][:2000]
        if total + len(text) > (12000 if only else 9000):
            break
        total += len(text)
        where = f", {'at ' if h['kind'] == 'video' else ''}{h['loc']}" if h["loc"] else ""
        parts.append(f"[{n}] {h['title']}{where}:\n{text}")
    if only:
        ctx = ("\n\nThe user wants answers taken from their own uploaded documents. Passages from their library:\n\n"
               + "\n\n".join(parts) +
               "\n\nAnswer using only these passages, and say which source you used (title, page or timestamp) in "
               "plain words. If they don't contain the answer, say you couldn't find it in the documents instead of "
               "using general knowledge, unless the user asks you to.")
    else:
        ctx = ("\n\nReference material from the user's library that may help answer the latest question:\n\n"
               + "\n\n".join(parts) +
               "\n\nUse it when it's relevant and mention which source you used (title, page or timestamp) in "
               "plain words. If it doesn't cover the question, answer from your own knowledge and say so briefly.")
    sources = [{"item_id": h["item_id"], "title": h["title"], "loc": h["loc"], "start": h["start"],
                "kind": h["kind"]} for h in hits[: len(parts)]]
    return ctx, sources


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Stream an Ollama chat reply back as NDJSON. First line: {"sources": [...]} from library search."""
    import requests
    messages = [dict(m) for m in req.messages]
    try:
        ctx, sources = library_context(req)
    except Exception as e:
        print("Library search failed:", e)
        ctx, sources = "", []
    if ctx:
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = messages[0].get("content", "") + ctx
        else:
            messages.insert(0, {"role": "system", "content": ctx.strip()})
    payload = {"model": req.model, "messages": messages, "stream": True,
               "think": False, "keep_alive": "30m", "options": {"num_ctx": OLLAMA_NUM_CTX}}
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=(5, 600))
        if r.status_code == 400 and "think" in r.text:      # model/older Ollama without think support
            payload.pop("think")
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=(5, 600))
    except Exception:
        raise HTTPException(503, f"Can't reach Ollama at {OLLAMA_URL} - is it running?")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", r.text)
        except Exception:
            msg = r.text
        raise HTTPException(r.status_code, f"Ollama: {msg}")

    def relay():
        try:
            yield (json.dumps({"sources": sources}) + "\n").encode()
            for line in r.iter_lines():
                if line:
                    yield line + b"\n"
        finally:
            r.close()

    return StreamingResponse(relay(), media_type="application/x-ndjson")


# ---------- Library ----------

def _bad(e: Exception):
    raise HTTPException(400, str(e))


class FolderReq(BaseModel):
    path: str


class FolderMove(BaseModel):
    src: str
    dst: str


class ItemPatch(BaseModel):
    folder: str | None = None
    title: str | None = None


class YoutubeReq(BaseModel):
    url: str
    folder: str = ""


class LocationReq(BaseModel):
    path: str


@app.get("/api/library")
def library_list():
    return {"root": str(library.root()), "folders": library.list_folders(), "items": library.list_items()}


@app.post("/api/folders")
def folder_create(req: FolderReq):
    try:
        return {"path": library.create_folder(req.path)}
    except ValueError as e:
        _bad(e)


@app.post("/api/folders/move")
def folder_move(req: FolderMove):
    try:
        return {"path": library.move_folder(req.src, req.dst)}
    except (ValueError, OSError) as e:
        _bad(e)


@app.delete("/api/folders")
def folder_delete(path: str, recursive: bool = False):
    try:
        library.delete_folder(path, recursive)
        return {"ok": True}
    except (ValueError, OSError) as e:
        _bad(e)


@app.put("/api/upload")
async def upload(request: Request, name: str, folder: str = ""):
    """Raw file body. Saves into the library folder and queues transcription/extraction."""
    if not library.kind_for(name):
        raise HTTPException(400, f"Unsupported file type: {name}")
    tmp_dir = library.root() / ".downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"upload-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        return {"id": library.add_file(tmp, folder, os.path.basename(name))}
    except ValueError as e:
        _bad(e)
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/youtube")
def youtube(req: YoutubeReq):
    if not re.search(r"(youtube\.com|youtu\.be)/", req.url):
        raise HTTPException(400, "That doesn't look like a YouTube link")
    try:
        return {"id": library.add_youtube(req.url, req.folder)}
    except ValueError as e:
        _bad(e)


@app.get("/api/items/{item_id}")
def item_get(item_id: int):
    item = library.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    return item


@app.patch("/api/items/{item_id}")
def item_patch(item_id: int, req: ItemPatch):
    try:
        return library.move_item(item_id, req.folder, req.title)
    except (ValueError, OSError) as e:
        _bad(e)


@app.delete("/api/items/{item_id}")
def item_delete(item_id: int):
    library.delete_item(item_id)
    return {"ok": True}


@app.post("/api/items/{item_id}/retry")
def item_retry(item_id: int):
    item = library.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    library.update_item(item_id, status="queued", progress=0, error=None)
    library.enqueue(item_id, item["kind"])
    return {"ok": True}


@app.get("/api/items/{item_id}/file")
def item_file(item_id: int):
    item = library.get_item(item_id)
    if not item or library.is_pending(item):
        raise HTTPException(404, "Not found")
    path = library.abs_path(item["rel_path"])
    if not path.exists():
        raise HTTPException(404, "File is missing - try Rescan")
    return FileResponse(path)


@app.get("/api/items/{item_id}/segments")
def item_segments(item_id: int, since: int = 0):
    return library.segments(item_id, since)


@app.get("/api/items/{item_id}/text")
def item_text(item_id: int):
    text = library.document_text(item_id)
    if text is None:
        raise HTTPException(404, "No text for this item yet")
    return {"text": text}


@app.get("/api/search")
def search(q: str, folder: str = ""):
    try:
        return library.search(q, folder, limit=10)
    except ValueError as e:
        _bad(e)


@app.post("/api/library/rescan")
def rescan():
    return library.rescan()


@app.post("/api/library/open")
def open_folder(path: str = ""):
    """Open a library folder in File Explorer (the server runs on your own PC)."""
    import subprocess
    import sys
    try:
        target = library.abs_path(path)
    except ValueError as e:
        _bad(e)
    if sys.platform == "win32":
        os.startfile(target)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return {"ok": True}


@app.post("/api/library/location")
def relocate(req: LocationReq):
    try:
        return library.relocate(req.path)
    except (ValueError, OSError) as e:
        _bad(e)


# ---------- Conversations (saved in SQLite) ----------

class ConvCreate(BaseModel):
    title: str = "New conversation"
    folder: str | None = None
    item_id: int | None = None


class ConvPatch(BaseModel):
    title: str


class MessageReq(BaseModel):
    role: str
    content: str
    video_time: float | None = None
    interrupted: bool = False


@app.get("/api/conversations")
def conv_list():
    with db.db() as c:
        return db.rows(c.execute(
            "SELECT c.id, c.title, c.folder, c.item_id, c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS count "
            "FROM conversations c ORDER BY c.updated_at DESC LIMIT 200"))


@app.post("/api/conversations")
def conv_create(req: ConvCreate):
    t = db.now()
    with db.db() as c:
        cur = c.execute("INSERT INTO conversations(title, folder, item_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)", (req.title[:200], req.folder, req.item_id, t, t))
        return {"id": cur.lastrowid}


@app.get("/api/conversations/{cid}")
def conv_get(cid: int):
    with db.db() as c:
        conv = c.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        if not conv:
            raise HTTPException(404, "Not found")
        msgs = db.rows(c.execute("SELECT id, role, content, video_time, interrupted, created_at FROM messages "
                                 "WHERE conversation_id = ? ORDER BY id", (cid,)))
    return {**dict(conv), "messages": msgs}


@app.patch("/api/conversations/{cid}")
def conv_rename(cid: int, req: ConvPatch):
    with db.db() as c:
        c.execute("UPDATE conversations SET title = ? WHERE id = ?", (req.title[:200], cid))
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def conv_delete(cid: int):
    with db.db() as c:
        c.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    return {"ok": True}


@app.post("/api/conversations/{cid}/messages")
def conv_add(cid: int, req: MessageReq):
    if req.role not in ("user", "assistant"):
        raise HTTPException(400, "role must be user or assistant")
    t = db.now()
    with db.db() as c:
        if not c.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
            raise HTTPException(404, "Conversation not found")
        c.execute("INSERT INTO messages(conversation_id, role, content, video_time, interrupted, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (cid, req.role, req.content, req.video_time, int(req.interrupted), t))
        c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (t, cid))
        # name the conversation after its first question
        c.execute("UPDATE conversations SET title = ? WHERE id = ? AND title = 'New conversation' AND ? = 'user'",
                  (req.content[:80] + ("…" if len(req.content) > 80 else ""), cid, req.role))
    return {"ok": True}


# ---------- startup ----------

def whisper_segments(audio) -> list[dict]:
    """Timestamped transcription of up to a few minutes of 16 kHz audio (used for library videos)."""
    with _stt_lock:
        out = get_asr()({"raw": audio, "sampling_rate": 16000}, chunk_length_s=30, batch_size=8,
                        return_timestamps=True,
                        generate_kwargs={"language": WHISPER_LANGUAGE, "task": "transcribe"})
    segs = []
    for ch in out.get("chunks") or []:
        s, e = ch.get("timestamp") or (0, None)
        segs.append({"start": s or 0, "end": e, "text": ch.get("text") or ""})
    return segs


db.init()
library.transcribe_audio = whisper_segments


@app.on_event("startup")
def warm_up():
    library.start_workers()

    # Load the models in the background so the first Play / first words are quick.
    def _load():
        try:
            with _lock:
                get_pipeline("af_heart")
            print(f"Kokoro ready on {_device}")
        except Exception as e:
            print("Kokoro warm-up failed:", e)
        try:
            with _stt_lock:
                get_asr()
            print(f"Whisper ready ({WHISPER_MODEL})")
        except Exception as e:
            print("Whisper warm-up failed:", e)
    if not os.environ.get("LOCAL_TTS_NO_WARMUP"):
        threading.Thread(target=_load, daemon=True).start()


# Serve the web page (no build step needed).
WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/{name}.js")
def script(name: str):
    f = WEB / f"{name}.js"
    if not name.isalnum() or not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="text/javascript")
