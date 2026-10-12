# Local TTS

A voice app that runs entirely on your PC. It reads text aloud, lets you have spoken
conversations with your [Ollama](https://ollama.com) models, and keeps a library of videos
(including YouTube downloads) and documents that the bot searches to answer your questions.

- **Voice:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
- **Hearing you:** [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo)
- **Answers:** your local Ollama models

Nothing leaves your machine, except when you load a webpage or download a YouTube video.

## Requirements

**Install yourself**

| Needs | For | Notes |
|---|---|---|
| Windows 10 or 11 | Everything | The scripts are `.bat` files. |
| An NVIDIA GPU with a current driver | Fast voice and speech recognition | Strongly recommended. Setup installs the CUDA 12.8 build of PyTorch, which RTX 50-series cards need. It runs on the CPU without one, but slowly. |
| [Ollama](https://ollama.com) with a chat model pulled | The Talk tab | Must be running. Read and Library work without it. |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng/releases) (`.msi`) | Pronouncing unusual words and names | Optional but recommended. |
| About 10 GB of free disk space | PyTorch (about 2.7 GB), the Kokoro voice (about 330 MB), Whisper (about 1.6 GB) and Python packages | Videos you add to the library need more. |
| An internet connection | Setup, first-run model downloads, loading webpages, YouTube downloads | Everything else runs offline. |

**Installed for you by `setup.bat`**

| Component | How |
|---|---|
| **Python 3.12** (Kokoro doesn't support 3.13 yet) | Installed with `winget` if it's missing. If winget isn't available, install it yourself from https://www.python.org/downloads/release/python-3128/ and tick *"Add python.exe to PATH"*. |
| PyTorch (CUDA 12.8 build) | `pip`, into a local `.venv` folder |
| Python packages in [`server/requirements.txt`](server/requirements.txt): `kokoro`, `transformers` (Whisper), `fastapi`, `uvicorn`, `python-multipart`, `soundfile`, `numpy`, `pypdf`, `python-docx`, `requests`, `beautifulsoup4`, `yt-dlp` | `pip`, into the same `.venv` |
| ffmpeg (audio extraction and YouTube merging) | Bundled through the `imageio-ffmpeg` package. A system ffmpeg is used only if that one is missing. |

**Only if you hit this:** YouTube downloads sometimes need a JavaScript runtime. If the error
mentions one, install Deno with `winget install DenoLand.Deno` and restart the app.

## Quick start

1. Install **Ollama**, pull a model, and make sure it's running. Optionally install **eSpeak NG**
   (see [Requirements](#requirements)).
2. Double-click **`setup.bat`** (one time). It installs Python 3.12 if needed, then PyTorch and
   the other packages, and prints whether it found your GPU. This takes a while on a slow connection.
3. Double-click **`start.bat`**. It starts the server and opens http://localhost:48765.

The first run downloads the voice model (about 330 MB) and Whisper (about 1.6 GB) from Hugging
Face. After that it runs offline. Your browser asks for microphone access the first time you use
Talk. The chip in the top-right corner shows "GPU (…)" once the model is loaded.

## What it does

### Read tab

- Type or paste text, or open a **.txt, .md, .pdf, .docx or .html** file. You can also drag one onto the page.
- Paste a **webpage link** and it pulls out the article text.
- Reads long text in chunks and highlights the sentence it's reading. It prepares the next chunks
  ahead of time, so playback doesn't stop between them.
- Click any sentence to jump there. Press Space to play or pause.
- Choose from 28 American and British voices, at speeds from 0.5× to 2×.
- **Ask about this** switches to the Talk tab so you can ask questions about the text you loaded.

### Talk tab: hands-free voice chat

- Click **Start conversation** and just talk. It hears when you stop, answers out loud, and
  starts speaking the first sentence while it's still writing the rest.
- Talk over it to interrupt (or tap the mic circle). You can turn this off in Settings.
- **Mic sensitivity (noise gate):** drag the red marker on the level meter. Sounds quieter than
  the marker are ignored. It's a fixed level you set, in dB, and it's remembered. While the bot is
  talking, a dashed line shows the higher level it needs to hear you interrupt.
- **Mute mic** (or press **M**) so a cough or sneeze can't interrupt the bot or become a question.
- Pick any Ollama model. Models that aren't chat models, like image-model text encoders, are
  hidden from the list. The model loads into memory as soon as you pick it, so your first question
  isn't slow.
- Type a question instead if you'd rather not talk.
- Conversations are saved automatically. *Past conversations* is a list grouped by date with a
  search box. Click one to continue it, use the pencil to **rename** it, or the bin to delete it.

### Library tab

- Your library is a **real folder on your PC** (by default `library` next to this app). Create,
  rename, move and delete folders in the app, and the same happens on disk.
- **Upload files** (or drag them onto the page): videos, PDFs, Word docs, text and Markdown, and
  code files like .rs, .py and .js. Videos are transcribed with timestamps and documents are read
  in the background.
- **Paste a YouTube link** to download the video (up to 1080p MP4) into the current folder. It's
  transcribed automatically when the download finishes. Only download videos you have the right to save.
- Drag items onto a folder to move them, or use Rename, Move and Delete. Renaming changes the file
  name on disk too.
- **Search** everything in a folder. Click a result to jump to that moment in the video, or to read
  the document aloud.
- Documents have an **Ask** button that opens the Talk tab focused on that one file.
- **Change…** moves the whole library somewhere else, like another drive. **Rescan** picks up files
  you added, moved or deleted in File Explorer. **Open** shows the folder in File Explorer.

### How the bot uses your library

- When you ask something in Talk, it searches the folder you choose under *Look things up in*
  and uses the best-matching passages from your documents and video transcripts. Passages that
  only share a stray word with your question are ignored.
- By default it **answers only from your documents**. If nothing in your library matches, it says
  it couldn't find the answer instead of using general knowledge. You can change this in Settings.
  If your library is empty, it just chats normally.
- **Ask about one document:** use **Ask** on a library file, or **Ask about this** in the Read tab.
  A chip shows what you're asking about, and answers come from that document. Click the ✕ on the
  chip to go back to searching the library.
- Answers list their sources ("From your library: book (page 4), Lesson 3 (12:30)"). Click one to
  jump there. You can hide this line in Settings.

### Video companion (learning from videos)

- Pick a video from your library in the Talk tab, or click **Watch** in the Library tab. You can
  start watching while it's still transcribing.
- Picking a video starts it playing (you can turn this off in Settings), and pressing play starts
  the conversation automatically. The video pauses the moment you start talking. The bot answers
  using everything you've watched so far, with extra weight on the last 45 seconds, so "what did
  he mean by that?" works. Then the video resumes 3 seconds later, unless you ask a follow-up. If
  what it heard was only a cough or noise, it resumes by itself.
- **While the video plays** (set in Settings):
  - *Speakers: speak up to ask*: the mic stays on, but the noise gate is raised about 10 dB while
    the video plays, so only clearly louder speech counts.
  - *Headphones: normal listening*: just start talking mid-video. It also ignores anything that
    sounds like what the video just said.
- It can show short code examples on screen. It doesn't read the code aloud, it explains it.
- Open **Video transcript** to see the full text. Click any line to jump there.

### Settings tab

- **Video:** start playing when I pick a video, resume after the answer, and speakers or
  headphones mode.
- **Conversation:** let me interrupt it by talking, how long it waits before replying, whether
  answers come only from your documents, whether to show the sources line, and the
  personality/instructions, with a **Reset to default** button.
- All settings are saved in your browser.

## Project layout

```
setup.bat / start.bat     Windows scripts
update-youtube.bat        Updates the YouTube downloader
server/app.py             FastAPI server: voices, speech-to-text, Ollama chat, library + conversation API
server/library.py         Library folders on disk, YouTube downloads, transcription, search
server/db.py              SQLite schema (items, transcripts, search index, conversations, settings)
server/textutil.py        Text cleanup, sentence chunking, PDF/DOCX/HTML readers
web/index.html            The app page, Read tab and Settings tab (plain HTML/JS, no build step)
web/library.js            Library tab
web/talk.js               Talk tab: mic, speech detection, streaming replies, interruptions
web/video.js              Video companion: playback, transcript, context for the bot
data/local-tts.db         Your database (not in git)
library/                  Your library folder (not in git, movable from the Library tab)
```

**API** (if you want to use it from other apps): `POST /api/speak {text, voice, speed}` returns a
24 kHz WAV file. `GET /api/voices` lists the voices.

## Your data and git

Everything is stored in one SQLite database, `data/local-tts.db`: the list of library items,
video transcripts with timestamps, extracted document text, the search index (SQLite FTS5),
saved conversations and settings. The videos and documents themselves are normal files in your
library folder. To back up, copy both.

The `.gitignore` keeps `library/`, `data/` and `.venv/` out of the repository, so your files,
videos and conversations are never committed. If you moved the library elsewhere, it's outside
this folder and isn't affected.

## Configuration

Set these as environment variables before `start.bat`, or edit the top of `server/app.py`:

- `OLLAMA_URL`: where Ollama runs. The default is `http://127.0.0.1:11434`.
- `OLLAMA_NUM_CTX`: the context window. The default is 16384, which leaves room for long video
  transcripts. Lower it if your model plus Whisper doesn't fit in VRAM.
- `WHISPER_MODEL`: for example `openai/whisper-large-v3` for top accuracy, or
  `distil-whisper/distil-large-v3` for speed.
- `WHISPER_LANGUAGE`: the default is `english`.
- `LOCAL_TTS_DB`: where the database lives. The default is `data/local-tts.db`.

## Troubleshooting

- **Talk says "Ollama not running":** start Ollama, then reload the page.
- **It keeps hearing itself or the video:** use headphones, or drag the red marker on the level
  meter to the right (a higher noise gate).
- **It doesn't hear you, or misses short questions like "why?":** drag the red marker to the left,
  so it sits just above your background noise.
- **It cuts you off before you finish:** set *Pause before replying* to Long in Settings.
- **The first answer takes a long time:** Ollama is loading the model into memory. If another app
  uses the same Ollama, the two can keep unloading each other's models and your GPU memory fills
  up. Close the other app while you use Talk, or pick a smaller model.
- **It says it couldn't find something that is in a document:** it matches words from your
  question, so use terms from the document, or use **Ask** on that file. You can also switch
  Settings to fall back to general knowledge.
- **YouTube downloads fail:** YouTube changes often, so run `update-youtube.bat` to get the latest
  downloader. If the error mentions a "JavaScript runtime", install Deno (`winget install DenoLand.Deno`)
  and restart the app.
- **A file shows "Missing on disk":** you moved or renamed it outside the app. Click **Rescan**.
- **A video won't play:** browsers can't play some formats, like some .mkv and .avi files. Convert
  the video to .mp4 first.
- **The chip says CPU instead of GPU:** reinstall torch with
  `.venv\Scripts\pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu128`.
- **A PDF has no readable text:** it's a scanned image and needs OCR first.
- **Port 48765 is already in use:** change the port in `start.bat`, in both places.

## License

The code in this repository is released under the [MIT License](LICENSE).

It installs third-party packages and downloads models that have their own licenses, for example
Kokoro (Apache 2.0) and Whisper (MIT). A few, including eSpeak NG and the phonemizer package, are
GPL. They are installed on your machine by `setup.bat` and are not included in this repository.
Your library and database are kept out of git by `.gitignore`.
