"""Text extraction and chunking helpers (no model dependencies, easy to test)."""
import io
import re

MAX_CHUNK_CHARS = 400

_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"-\n(?=[a-z])", "", text)          # re-join hyphenated line breaks (PDFs)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)      # single newlines -> spaces
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    return [c["text"] for c in split_chunks_with_paras(text, max_chars)]


def split_chunks_with_paras(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    """Split into chunks of whole sentences (<= max_chars when possible), tagged with paragraph number."""
    chunks: list[dict] = []
    para_no = -1
    for para in re.split(r"\n\s*\n", clean_text(text)):
        para = para.strip()
        if not para:
            continue
        para_no += 1
        current = ""
        for sentence in _SENTENCE_END.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            # Very long sentence: break on commas/semicolons, then on words.
            pieces = [sentence]
            if len(sentence) > max_chars:
                pieces = _hard_split(sentence, max_chars)
            for piece in pieces:
                if current and len(current) + 1 + len(piece) > max_chars:
                    chunks.append({"text": current, "para": para_no})
                    current = piece
                else:
                    current = f"{current} {piece}".strip()
        if current:
            chunks.append({"text": current, "para": para_no})
    return chunks


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    out, current = [], ""
    for part in re.split(r"(?<=[,;:])\s+|\s+", sentence):
        if current and len(current) + 1 + len(part) > max_chars:
            out.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        out.append(current)
    return out


def extract_file(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if name.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if name.endswith((".html", ".htm")):
        return html_to_text(data.decode("utf-8", errors="replace"))[1]
    # .txt, .md and anything else text-like
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, readable text) from an HTML page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "button", "svg", "figure", "iframe"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        t = el.get_text(" ", strip=True)
        if len(t) > 1:
            blocks.append(t)
    if not blocks:
        blocks = [root.get_text(" ", strip=True)]
    return title, "\n\n".join(blocks)


def fetch_url(url: str) -> tuple[str, str]:
    import requests
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    resp = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalTTS/1.0"})
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return url.rsplit("/", 1)[-1], extract_file("x.pdf", resp.content)
    if "html" in ctype or not ctype:
        return html_to_text(resp.text)
    return url, resp.text
