"""The library: real folders on disk + metadata, transcripts and search in SQLite.

Every file lives under the library folder (changeable in the app). The database stores paths
relative to that folder, so the whole library can be moved without losing anything.
"""
import os
import queue
import re
import shutil
import subprocess
import threading
import traceback
import uuid
from pathlib import Path

import db
import textutil

VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi"}
DOC_EXT = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm", ".rst", ".csv"}
CODE_EXT = {".py", ".rs", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".c", ".h", ".cpp",
            ".hpp", ".cs", ".rb", ".php", ".swift", ".lua", ".zig", ".ex", ".exs", ".hs", ".scala", ".dart",
            ".sql", ".sh", ".ps1", ".toml", ".yaml", ".yml", ".json", ".css", ".vue", ".svelte"}
HIDDEN_DIRS = {".downloads", ".trash"}
PENDING = ".pending-"          # placeholder file name for YouTube items still downloading
ACTIVE_STATES = ("queued", "downloading", "transcribing", "extracting")

# Set by app.py: transcribe(np.float32 16 kHz audio) -> list of {"start", "end", "text"} (seconds).
transcribe_audio = None


# ---------------------------------------------------------------- paths

def default_root() -> Path:
    return db.APP_DIR / "library"


def root() -> Path:
    r = Path(db.get_setting("library_root") or default_root())
    r.mkdir(parents=True, exist_ok=True)
    return r


def norm_rel(rel: str | None) -> str:
    """Normalise a folder/file path relative to the library; rejects anything escaping it."""
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    if not rel:
        return ""
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", "..") or p.startswith(".") or re.search(r'[<>:"|?*\x00-\x1f]', p):
            raise ValueError(f"Invalid folder or file name: {p!r}")
    return "/".join(parts)


def abs_path(rel: str) -> Path:
    r = root()
    p = (r / norm_rel(rel)) if rel else r
    if r.resolve() not in (p.resolve(), *p.resolve().parents):
        raise ValueError("Path is outside the library")
    return p


def clean_name(name: str, max_len: int = 120) -> str:
    name = re.sub(r"\s*:\s*", " - ", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip().strip(".").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len].rstrip(" .-") or "untitled")


def unique_path(folder: Path, filename: str) -> Path:
    stem, ext = os.path.splitext(filename)
    p, n = folder / filename, 2
    while p.exists():
        p, n = folder / f"{stem} ({n}){ext}", n + 1
    return p


def rel_of(p: Path) -> str:
    return p.resolve().relative_to(root().resolve()).as_posix()


def parent_rel(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def kind_for(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT or ext in CODE_EXT:
        return "document"
    return None


# ---------------------------------------------------------------- listing

def list_folders() -> list[str]:
    r = root()
    out = []
    for dirpath, dirnames, _ in os.walk(r):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in HIDDEN_DIRS)
        rel = Path(dirpath).relative_to(r).as_posix()
        if rel != ".":
            out.append(rel)
    return out


ITEM_COLS = ("id, kind, title, rel_path, folder, source_url, size, duration, status, progress, error, "
             "created_at, updated_at")


def list_items() -> list[dict]:
    with db.db() as c:
        return db.rows(c.execute(f"SELECT {ITEM_COLS} FROM items ORDER BY folder, title COLLATE NOCASE"))


def get_item(item_id: int) -> dict | None:
    with db.db() as c:
        r = c.execute(f"SELECT {ITEM_COLS} FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(r) if r else None


def update_item(item_id: int, **fields):
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.db() as c:
        c.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))


# ---------------------------------------------------------------- folders

def create_folder(rel: str) -> str:
    rel = norm_rel(rel)
    if not rel:
        raise ValueError("Folder name is empty")
    abs_path(rel).mkdir(parents=True, exist_ok=True)
    return rel


def move_folder(src: str, dst: str) -> str:
    """Rename or move a folder (and everything in it), on disk and in the database."""
    src, dst = norm_rel(src), norm_rel(dst)
    if not src or not dst:
        raise ValueError("Can't move the library's top folder")
    if dst == src:
        return dst
    if dst.startswith(src + "/"):
        raise ValueError("Can't move a folder inside itself")
    s, d = abs_path(src), abs_path(dst)
    if not s.is_dir():
        raise ValueError("Folder not found")
    if d.exists() and not (os.path.normcase(str(s)) == os.path.normcase(str(d))):
        raise ValueError(f"'{dst}' already exists")
    d.parent.mkdir(parents=True, exist_ok=True)
    os.rename(s, d)
    n = len(src)
    with db.db() as c:
        c.execute("UPDATE items SET rel_path = ? || substr(rel_path, ?) WHERE substr(rel_path, 1, ?) = ?",
                  (dst, n + 1, n + 1, src + "/"))
        c.execute("UPDATE items SET folder = ? || substr(folder, ?) WHERE folder = ? OR substr(folder, 1, ?) = ?",
                  (dst, n + 1, src, n + 1, src + "/"))
        c.execute("UPDATE conversations SET folder = ? || substr(folder, ?) "
                  "WHERE folder = ? OR substr(folder, 1, ?) = ?", (dst, n + 1, src, n + 1, src + "/"))
    return dst


def delete_folder(rel: str, recursive: bool = False):
    rel = norm_rel(rel)
    if not rel:
        raise ValueError("Can't delete the library's top folder")
    p = abs_path(rel)
    if not p.is_dir():
        raise ValueError("Folder not found")
    if any(p.iterdir()) and not recursive:
        raise ValueError("Folder isn't empty")
    with db.db() as c:
        c.execute("DELETE FROM items WHERE folder = ? OR substr(folder, 1, ?) = ?", (rel, len(rel) + 1, rel + "/"))
    shutil.rmtree(p)


# ---------------------------------------------------------------- items

def _insert_item(kind, title, rel_path, source_url=None, size=None, status="queued") -> int:
    t = db.now()
    with db.db() as c:
        cur = c.execute(
            "INSERT INTO items(kind, title, rel_path, folder, source_url, size, status, progress, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (kind, title, rel_path, parent_rel(rel_path), source_url, size, status, t, t))
        return cur.lastrowid


def add_file(temp_path: Path, folder: str, filename: str) -> int:
    """Move an uploaded file into a library folder and queue it for processing."""
    kind = kind_for(filename)
    if not kind:
        raise ValueError(f"Unsupported file type: {filename}")
    dest_dir = abs_path(norm_rel(folder))
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    dest = unique_path(dest_dir, clean_name(stem) + ext.lower())
    shutil.move(str(temp_path), dest)
    item_id = _insert_item(kind, Path(dest).stem, rel_of(dest), size=dest.stat().st_size)
    enqueue(item_id, kind)
    return item_id


def add_youtube(url: str, folder: str) -> int:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    folder = norm_rel(folder)
    abs_path(folder).mkdir(parents=True, exist_ok=True)
    placeholder = (folder + "/" if folder else "") + PENDING + uuid.uuid4().hex[:8]
    item_id = _insert_item("video", "Downloading…", placeholder, source_url=url)
    enqueue(item_id, "video")
    return item_id


def is_pending(item: dict) -> bool:
    return item["rel_path"].rsplit("/", 1)[-1].startswith(PENDING)


def move_item(item_id: int, folder: str | None = None, title: str | None = None) -> dict:
    """Move an item to another folder and/or rename it (renames the file on disk too)."""
    item = get_item(item_id)
    if not item:
        raise ValueError("Item not found")
    if is_pending(item):
        raise ValueError("Wait for the download to finish first")
    folder = item["folder"] if folder is None else norm_rel(folder)
    title = clean_name(title) if title else item["title"]
    src = abs_path(item["rel_path"])
    if not src.exists():
        raise ValueError("The file is missing on disk. Use Rescan to find it.")
    dest_dir = abs_path(folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix
    dest = dest_dir / f"{title}{ext}"
    if os.path.normcase(str(dest)) != os.path.normcase(str(src)):
        if dest.exists():
            dest = unique_path(dest_dir, f"{title}{ext}")
        shutil.move(str(src), dest)
    update_item(item_id, rel_path=rel_of(dest), folder=folder, title=Path(dest).stem)
    return get_item(item_id)


def delete_item(item_id: int):
    item = get_item(item_id)
    if not item:
        return
    try:
        if not is_pending(item):
            abs_path(item["rel_path"]).unlink(missing_ok=True)
    finally:
        with db.db() as c:
            c.execute("DELETE FROM items WHERE id = ?", (item_id,))


def segments(item_id: int, since: int = 0) -> list[dict]:
    with db.db() as c:
        return db.rows(c.execute("SELECT seq, start, end, text FROM segments WHERE item_id = ? AND seq >= ? "
                                 "ORDER BY seq", (item_id, since)))


def document_text(item_id: int) -> str | None:
    with db.db() as c:
        r = c.execute("SELECT text FROM documents WHERE item_id = ?", (item_id,)).fetchone()
    return r["text"] if r else None


# ---------------------------------------------------------------- rescan / relocate

def rescan() -> dict:
    """Sync the database with what's on disk: import new files, follow files moved in Explorer,
    and mark missing ones."""
    r = root()
    on_disk = {}
    for dirpath, dirnames, filenames in os.walk(r):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in HIDDEN_DIRS]
        for f in filenames:
            if f.startswith(".") or not kind_for(f):
                continue
            p = Path(dirpath) / f
            on_disk[p.relative_to(r).as_posix()] = p.stat().st_size
    items = list_items()
    known = {i["rel_path"] for i in items}
    new = {rel: size for rel, size in on_disk.items() if rel not in known}
    stats = {"added": 0, "moved": 0, "missing": 0, "found": 0}
    for it in items:
        if is_pending(it):
            continue
        if it["rel_path"] in on_disk:
            if it["status"] == "missing":
                stats["found"] += 1
                _restore(it)
            continue
        # Same file name + size somewhere else = moved outside the app.
        name = it["rel_path"].rsplit("/", 1)[-1]
        match = next((rel for rel, size in new.items()
                      if rel.rsplit("/", 1)[-1] == name and size == it["size"]), None)
        if match:
            new.pop(match)
            update_item(it["id"], rel_path=match, folder=parent_rel(match))
            if it["status"] == "missing":
                _restore(it)
            stats["moved"] += 1
        elif it["status"] != "missing":
            update_item(it["id"], status="missing")
            stats["missing"] += 1
    for rel, size in new.items():
        kind = kind_for(rel)
        item_id = _insert_item(kind, Path(rel).stem, rel, size=size)
        enqueue(item_id, kind)
        stats["added"] += 1
    return stats


def _restore(item):
    with db.db() as c:
        has_data = c.execute("SELECT 1 FROM chunks WHERE item_id = ? LIMIT 1", (item["id"],)).fetchone()
    if has_data:
        update_item(item["id"], status="ready", progress=1)
    else:
        update_item(item["id"], status="queued", progress=0)
        enqueue(item["id"], item["kind"])


def relocate(new_root: str, move_files: bool = True) -> dict:
    """Change where the library lives. Moves everything there (merging if it has files already)."""
    new = Path(os.path.expanduser(new_root.strip().strip('"'))).resolve()
    old = root().resolve()
    if new == old:
        return {"root": str(new)}
    if old in new.parents:
        raise ValueError("The new location can't be inside the current library")
    new.mkdir(parents=True, exist_ok=True)
    if move_files:
        with db.db() as c:
            busy = c.execute("SELECT COUNT(*) FROM items WHERE status IN ('downloading','transcribing','extracting')"
                             ).fetchone()[0]
        if busy:
            raise ValueError("Wait for downloads and transcriptions to finish first")
        for entry in list(old.iterdir()):
            target = new / entry.name
            if target.exists():
                if entry.is_dir() and target.is_dir():
                    _merge_dirs(entry, target)
                    continue
                target = unique_path(new, entry.name)
            shutil.move(str(entry), target)
    db.set_setting("library_root", str(new))
    return {"root": str(new), **rescan()}


def _merge_dirs(src: Path, dst: Path):
    for entry in list(src.iterdir()):
        target = dst / entry.name
        if target.exists() and entry.is_dir() and target.is_dir():
            _merge_dirs(entry, target)
        else:
            shutil.move(str(entry), target if not target.exists() else unique_path(dst, entry.name))
    src.rmdir()


# ---------------------------------------------------------------- background jobs

_queues = {"video": queue.Queue(), "document": queue.Queue()}


def enqueue(item_id: int, kind: str):
    _queues[kind].put(item_id)


def start_workers():
    for kind, q in _queues.items():
        threading.Thread(target=_worker, args=(q,), daemon=True, name=f"{kind}-worker").start()
    # Resume anything that was in progress when the app last closed.
    with db.db() as c:
        pending = db.rows(c.execute(f"SELECT id, kind FROM items WHERE status IN {ACTIVE_STATES} ORDER BY id"))
    for it in pending:
        enqueue(it["id"], it["kind"])


def _worker(q: queue.Queue):
    while True:
        item_id = q.get()
        try:
            item = get_item(item_id)
            if not item or item["status"] in ("ready", "missing"):
                continue
            if item["kind"] == "video":
                if is_pending(item):
                    _download(item)
                    item = get_item(item_id)
                if item:
                    _transcribe(item)
            else:
                _extract(item)
        except Exception as e:
            traceback.print_exc()
            if get_item(item_id):
                update_item(item_id, status="error", error=str(e)[:500])
        finally:
            q.task_done()


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def _download(item: dict):
    import yt_dlp
    update_item(item["id"], status="downloading", progress=0, error=None)
    tmp = root() / ".downloads" / str(item["id"])
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    state = {"files": set()}

    def hook(d):
        if d.get("status") == "downloading":
            state["files"].add(d.get("filename"))
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                # video + audio are separate downloads; show each as half the bar
                part = (len(state["files"]) - 1) * 0.5
                frac = d.get("downloaded_bytes", 0) / total
                update_item(item["id"], progress=round(min(0.99, part + frac * 0.5), 3))

    opts = {
        "format": "bv*[ext=mp4][vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(tmp / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "windowsfilenames": True, "ffmpeg_location": ffmpeg_exe(), "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(item["source_url"], download=True)
    files = [p for p in tmp.iterdir() if p.suffix.lower() in VIDEO_EXT]
    if not files:
        raise RuntimeError("Download finished but no video file was produced")
    video = max(files, key=lambda p: p.stat().st_size)
    title = clean_name(info.get("title") or video.stem)
    dest_dir = abs_path(item["folder"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest_dir, title + video.suffix.lower())
    shutil.move(str(video), dest)
    shutil.rmtree(tmp, ignore_errors=True)
    update_item(item["id"], rel_path=rel_of(dest), folder=item["folder"], title=dest.stem,
                size=dest.stat().st_size, duration=info.get("duration"), status="queued", progress=0)


def decode_audio_16k(path: Path):
    import numpy as np
    proc = subprocess.run([ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("Couldn't read the audio: " + proc.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)


def _transcribe(item: dict):
    path = abs_path(item["rel_path"])
    update_item(item["id"], status="transcribing", progress=0, error=None)
    with db.db() as c:                                   # start fresh (e.g. resumed after a restart)
        c.execute("DELETE FROM segments WHERE item_id = ?", (item["id"],))
        c.execute("DELETE FROM chunks WHERE item_id = ?", (item["id"],))
    audio = decode_audio_16k(path)
    if audio.size < 16000:
        raise RuntimeError("No audio found in this video")
    duration = audio.size / 16000
    update_item(item["id"], duration=round(duration, 2))
    piece = 16000 * 120                                   # 2-minute pieces: progress + lets Talk mode in
    seq = cseq = 0
    for start in range(0, audio.size, piece):
        part = audio[start:start + piece]
        if part.size < 8000:
            break
        offset = start / 16000
        segs = []
        for s in transcribe_audio(part):
            text = s["text"].strip()
            if text:
                st = offset + (s["start"] or 0)
                en = offset + s["end"] if s["end"] is not None else min(offset + part.size / 16000, st + 5)
                segs.append((round(st, 2), round(en, 2), text))
        # ~60-second searchable windows
        windows, cur = [], []
        for sg in segs:
            if cur and sg[0] - cur[0][0] >= 60:
                windows.append(cur)
                cur = []
            cur.append(sg)
        if cur:
            windows.append(cur)
        with db.db() as c:
            for st, en, text in segs:
                c.execute("INSERT INTO segments(item_id, seq, start, end, text) VALUES (?, ?, ?, ?, ?)",
                          (item["id"], seq, st, en, text))
                seq += 1
            for w in windows:
                c.execute("INSERT INTO chunks(item_id, seq, loc, start, text) VALUES (?, ?, ?, ?, ?)",
                          (item["id"], cseq, fmt_time(w[0][0]), w[0][0], " ".join(x[2] for x in w)))
                cseq += 1
        update_item(item["id"], progress=round(min(1.0, (start + piece) / audio.size), 3))
    update_item(item["id"], status="ready", progress=1)


def fmt_time(t: float) -> str:
    t = int(t or 0)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract(item: dict):
    path = abs_path(item["rel_path"])
    update_item(item["id"], status="extracting", progress=0, error=None)
    data = path.read_bytes()
    ext = path.suffix.lower()
    sections: list[tuple[str | None, str]] = []      # (location label, text)
    if ext == ".pdf":
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        for i, page in enumerate(reader.pages, 1):
            t = textutil.clean_text(page.extract_text() or "")
            if t:
                sections.append((f"page {i}", t))
    elif ext in CODE_EXT:
        lines = data.decode("utf-8", errors="replace").splitlines()
        for i in range(0, len(lines), 60):
            block = "\n".join(lines[i:i + 60]).strip()
            if block:
                sections.append((f"lines {i + 1}-{min(i + 60, len(lines))}", block))
    else:
        t = textutil.clean_text(textutil.extract_file(path.name, data))
        if t:
            sections.append((None, t))
    if not sections:
        raise RuntimeError("No readable text found (scanned PDFs need OCR first)")
    full = "\n\n".join(t for _, t in sections)
    chunks = []
    for loc, text in sections:
        for piece in _split_passages(text, 1200, keep_lines=ext in CODE_EXT):
            chunks.append((loc, piece))
    with db.db() as c:
        c.execute("DELETE FROM chunks WHERE item_id = ?", (item["id"],))
        c.execute("INSERT INTO documents(item_id, text) VALUES (?, ?) "
                  "ON CONFLICT(item_id) DO UPDATE SET text = excluded.text", (item["id"], full))
        for i, (loc, text) in enumerate(chunks):
            c.execute("INSERT INTO chunks(item_id, seq, loc, text) VALUES (?, ?, ?, ?)", (item["id"], i, loc, text))
    update_item(item["id"], status="ready", progress=1, size=path.stat().st_size)


def _split_passages(text: str, max_chars: int, keep_lines: bool = False) -> list[str]:
    if keep_lines:
        return [text] if len(text) <= max_chars * 2 else [text[i:i + max_chars * 2] for i in range(0, len(text), max_chars * 2)]
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            for sent in textutil.split_chunks(para, max_chars):
                if cur and len(cur) + len(sent) + 1 > max_chars:
                    out.append(cur)
                    cur = ""
                cur = f"{cur} {sent}".strip()
            continue
        if cur and len(cur) + len(para) + 2 > max_chars:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}".strip()
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- search

STOPWORDS = set("""a an the and or but if then so of to in on at by for with about from into over under is are was were
be been being am do does did done have has had having i me my we our you your he she it its they them their this that
these those there here what which who whom whose when where why how can could should would will shall may might must
not no yes just also very really like get got make made let lets tell explain mean means meant say said says
okay ok um uh please thanks thank know want wanted need again more some any all one""".split())


def search(query: str, folder: str = "", limit: int = 6, video_id: int | None = None,
           video_time: float | None = None, item_id: int | None = None) -> list[dict]:
    """Full-text (BM25) search over passages in a folder and its subfolders (or within one item)."""
    terms = [t for t in re.findall(r"[a-z0-9_+#]+", query.lower()) if t not in STOPWORDS and len(t) > 1]
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', "") + '"' for t in dict.fromkeys(terms))
    folder = norm_rel(folder)
    sql = ("SELECT c.id, c.item_id, c.loc, c.start, c.text, i.title, i.kind, i.folder, bm25(chunks_fts) AS score "
           "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN items i ON i.id = c.item_id "
           "WHERE chunks_fts MATCH ?")
    args: list = [match]
    if item_id is not None:
        sql += " AND c.item_id = ?"
        args.append(item_id)
    elif folder:
        sql += " AND (i.folder = ? OR substr(i.folder, 1, ?) = ?)"
        args += [folder, len(folder) + 1, folder + "/"]
    if video_id is not None:                 # don't jump ahead in the video being watched
        sql += " AND NOT (c.item_id = ? AND c.start > ?)"
        args += [video_id, (video_time or 0) + 5]
    sql += " ORDER BY score LIMIT ?"
    args.append(limit)
    with db.db() as c:
        return db.rows(c.execute(sql, args))


def item_opening(item_id: int, limit: int = 2) -> list[dict]:
    """The first passages of an item (for 'summarize this' style questions that match no keywords)."""
    with db.db() as c:
        return db.rows(c.execute(
            "SELECT c.id, c.item_id, c.loc, c.start, c.text, i.title, i.kind, i.folder "
            "FROM chunks c JOIN items i ON i.id = c.item_id WHERE c.item_id = ? ORDER BY c.id LIMIT ?",
            (item_id, limit)))
