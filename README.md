<p align="center">
  <img src="assets/lumen-icon.png" alt="Lumen open-book mark" width="168">
</p>

<h1 align="center">Lumen Book Reader</h1>

<p align="center">
  <strong>A focused desktop reading room for EPUB and PDF—built to help ideas land.</strong>
  <br>
  Faithful pages, deep definitions, durable notes, and a spectacular RSVP speed reader.
</p>

<p align="center">
  <a href="https://github.com/XAIHT/Lumen-Book-Reader"><img alt="Lumen 1.1.0" src="https://img.shields.io/badge/LUMEN-v1.1.0-63d1ad?style=for-the-badge&labelColor=111620"></a>
  <a href="https://www.python.org/"><img alt="Python 3.10+" src="https://img.shields.io/badge/PYTHON-3.10+-4381b3?style=for-the-badge&labelColor=111620"></a>
  <img alt="EPUB and PDF" src="https://img.shields.io/badge/READS-EPUB_·_PDF-f2bd4d?style=for-the-badge&labelColor=111620">
  <img alt="RSVP speed reading" src="https://img.shields.io/badge/CROWN_JEWEL-RSVP-ff7c52?style=for-the-badge&labelColor=111620">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/LICENSE-MIT-8b7cf6?style=for-the-badge&labelColor=111620"></a>
</p>

<p align="center">
  <a href="#-the-crown-jewel-rsvp-speed-reading">RSVP</a> ·
  <a href="#-definitions-that-understand-the-page">Deep definitions</a> ·
  <a href="#-ollama-pro-setup">Ollama Pro</a> ·
  <a href="#-install-and-run">Install</a> ·
  <a href="SpeedReadingToolInLumenReader.md">RSVP design notes</a>
</p>

![The current Lumen reader showing a faithful PDF and its live definition card](docs/screenshot.png)

Lumen gives books a calm native shell without flattening what makes them books. EPUBs stay responsive and themeable. PDFs retain their original typography, color, artwork, rotation, and layout while gaining a transparent selectable text layer for search, quotations, definitions, notes, and speed reading.

| The experience | What Lumen actually does |
|---|---|
| ⚡ **RSVP speed reading** | Presents a book at one stable focus point, beginning from the exact place where you were reading. |
| ◇ **Definitions in context** | Resolves words and phrases with offline, conventional online, inferred, and optional expert sources—without leaving the page. |
| ✦ **Reading memory** | Restores position and keeps searchable notes, quotations, tags, and marks across the whole shelf. |
| 📖 **EPUB + PDF fidelity** | Sanitized, themeable EPUB; high-resolution original-page PDF rendering with aligned selectable text. |

## ⚡ The crown jewel: RSVP speed reading

![Lumen RSVP welcoming the reader before its three-second countdown](docs/rsvp-speed-reader.png)

Lumen’s **Rapid Serial Visual Presentation (RSVP)** player turns the complete text of either an EPUB or PDF into a fixed-focus stream. Press **Ctrl+Shift+R**, tune the session in **Speed Reader Studio**, and Lumen returns you to the corresponding reading position when the session ends.

- A welcoming, non-skippable **3 → 2 → 1** transition prepares the eyes before the first word.
- **80–1200 WPM**, **1–5 words per fixation**, and live ±25 WPM adjustment.
- A colored optimal-recognition letter and fixation guides keep the gaze stable.
- Sentence, clause, and long-word timing prevent a mechanically flat rhythm.
- Configurable dark intervals, high-contrast colors, type size, fullscreen, and minimal chrome.
- Optional eye-rest reminders, seekable progress, and roughly ten-second jumps.
- Mouse-wheel movement scrolls the settings page—it never silently changes a field.

| While RSVP is open | Control |
|---|---|
| Pause / resume | <kbd>Space</kbd> or click the word |
| Jump about 10 seconds | <kbd>←</kbd> / <kbd>→</kbd> |
| Change speed by 25 WPM | <kbd>↑</kbd> / <kbd>↓</kbd> |
| Return to page reading | <kbd>Esc</kbd> |

RSVP can be useful for fluent review, but speed and comprehension are not the same thing. Begin conservatively, pause when meaning becomes unclear, and use normal page reading for close study. The rationale, limits, comfort guidance, and research references are in [SpeedReadingToolInLumenReader.md](SpeedReadingToolInLumenReader.md).

## ◇ Definitions that understand the page

Double-click a word, or select a phrase and choose the definition prompt. Lumen runs an append-only, clearly labeled source ladder:

1. bundled Princeton WordNet and the local cache;
2. Wiktionary plus DictionaryAPI.dev, Wikipedia, or Datamuse when appropriate;
3. transparent contextual morphology for coined or inflected expressions;
4. optional Tlamatini Googler evidence;
5. optional **Ollama contextual resolution**, only after conventional sources miss.

That last stage is why Ollama exists in Lumen: **not to chat, summarize the book, or replace a dictionary, but to resolve a difficult definition from the passage being read**. Lumen supplies the selected expression, a bounded surrounding passage, and the current book/section titles; validates the structured answer; and labels the model that produced it.

## ☁ Ollama Pro setup

**Recommended for frequent cloud definitions.** Ollama’s Pro plan currently costs **$20/month or $200/year**, includes larger cloud models, three concurrent cloud models, and 50× the Free cloud allowance. Lumen itself is free and does not require Pro; the subscription is purchased from Ollama. See [Ollama pricing](https://ollama.com/pricing).

### 1. Install Ollama

Download and install the current build from [ollama.com/download](https://ollama.com/download). On Windows, Ollama runs in the background and serves its local API at <code>http://localhost:11434</code>; macOS and Linux are also supported.

Confirm the command is available:

~~~powershell
ollama --version
~~~

### 2. Activate Pro and sign in

Subscribe through [Ollama Pro](https://ollama.com/pricing), then connect the local Ollama application to that account:

~~~powershell
ollama signin
~~~

The browser completes authentication. Lumen never asks for or stores the Ollama password, public key, or API key.

### 3. Pull and verify the cloud model

Lumen’s current default is <code>glm-5.2:cloud</code>:

~~~powershell
ollama pull glm-5.2:cloud
ollama run glm-5.2:cloud
~~~

After the test response, exit the interactive prompt with <code>/bye</code>. <code>ollama ls</code> should now list the cloud tag. Cloud models execute on Ollama’s hosted hardware, so a large local GPU is not required.

### 4. Connect Lumen

1. Start Lumen and select **◇ Definer**.
2. Enable **Ollama only after conventional sources miss**.
3. Keep **Ollama host** as <code>http://127.0.0.1:11434</code>.
4. Select **Discover models**, then choose <code>glm-5.2:cloud</code>.
5. Select **Save definition sources**.
6. While reading, double-click a difficult word or define a selected phrase.

The model list is discovered live because Ollama may retire cloud tags over time. If the default is no longer offered, pull another model from the official [cloud model library](https://ollama.com/search?c=cloud), discover again, and select it.

> **Privacy:** local dictionary and local-model requests stay on the machine. For a cloud model, the selected text, short surrounding context, book title, and section title are processed by Ollama Cloud. Ollama states that prompt/response content is not stored, logged, or used for training; review its current [cloud documentation](https://docs.ollama.com/cloud) and [privacy FAQ](https://docs.ollama.com/faq) before enabling the fallback.

If discovery fails, verify that the Ollama tray application is running, <code>ollama ls</code> works, the host remains local, and no proxy or firewall is blocking port <code>11434</code>.

## ✨ Everything else

- Search the current page or the complete book from the current position, forward or backward.
- Search the shelf by title, author, or filename; open by dialog, command line, or drag-and-drop.
- Browse nested EPUB navigation or an embedded PDF outline, with page fallback when no outline exists.
- Restore the exact section/page and within-page position.
- Keep notes and reading marks in the portable <code>lumen-reading-marks.json</code> file beside the library.
- Use Night, Paper, or Sepia themes; EPUB text scales from 14–32 px.
- Open password-protected PDFs without persisting the password.
- Recover text from image-only PDFs when optional [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) is installed on <code>PATH</code>.
- Scroll precisely with a wheel, touchpad, or touch gesture across the shelf, reader, sidebars, dialogs, and definition cards.

## 🚀 Install and run

Lumen requires Python 3.10 or newer.

~~~powershell
git clone https://github.com/XAIHT/Lumen-Book-Reader.git
cd Lumen-Book-Reader
python -m pip install -e .
lumen-reader
~~~

Or run directly from a checkout:

~~~powershell
python -m pip install -r requirements.txt
python run_reader.py
python run_reader.py "C:\Books\My Book.epub"
~~~

The launch directory becomes the visible shelf; recently opened books outside it are remembered too.

### Essential controls

| Action | Keyboard |
|---|---|
| Open a book | <kbd>Ctrl+O</kbd> |
| Search the open book | <kbd>Ctrl+F</kbd> |
| Next / previous page or section | <kbd>Page Down</kbd> / <kbd>Page Up</kbd> |
| Alternate next / previous | <kbd>Ctrl+→</kbd> / <kbd>Ctrl+←</kbd> |
| Start RSVP | <kbd>Ctrl+Shift+R</kbd> |
| Mark this position | <kbd>Ctrl+B</kbd> |
| Search all notes and marks | <kbd>Ctrl+Shift+M</kbd> |
| Return to the shelf | <kbd>Alt+←</kbd> |
| EPUB type size / reset | <kbd>Ctrl++</kbd> / <kbd>Ctrl+-</kbd> / <kbd>Ctrl+0</kbd> |
| Follow a book link | <kbd>Ctrl</kbd>+click |

## Trust, storage, and tests

Lumen treats book files as untrusted input. EPUB extraction rejects traversal and oversized payloads, active content is removed, rendered chapters use a restrictive content-security policy, and Chromium cannot navigate the reading surface from an ordinary click. PDF pages are rasterized from the original document, while only their aligned text is made interactive.

Reader state and the definition cache use the operating system’s application-data directory. Cross-book notes live in <code>lumen-reading-marks.json</code> in the launch directory so the reading library remains portable.

Run the complete regression suite:

~~~powershell
python -m pip install -e ".[test]"
python -m pytest
~~~

Coverage includes EPUB safety and rendering, PDF fidelity/rotation/passwords/selection, WordNet and online-response parsing, contextual Ollama payload validation, search order, notes, persistence, wheel-safe settings, and RSVP timing/countdown behavior.

## License

Lumen’s original code is [MIT licensed](LICENSE). Runtime components and optional integrations retain their own terms. In particular, **PyMuPDF is offered under AGPL or a commercial license**, which may affect redistribution of a packaged application. Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before distributing binaries.

<p align="center">
  <strong>Open a book. Find the meaning. Let the words move.</strong>
</p>
