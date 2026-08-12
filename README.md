# Lumen Book Reader

Lumen 1.1 is a polished desktop reader for EPUB and PDF documents. It combines a native PySide6 interface with a protected Qt WebEngine reading surface, persistent reading positions, a searchable cross-book notes system, and a context-aware definition workflow for both individual words and complete phrases.

EPUB books remain responsive and themeable. PDF pages retain their original typography, colors, images, vector artwork, orientation, and annotation appearance while gaining a transparent selectable text layer for definitions, search, quotations, and notes.

The application and Windows shortcut use an original, non-trademarked open-book icon stored in lumen_reader/assets.

![Lumen reading an included EPUB](docs/screenshot.png)

## Supported formats

| Capability | EPUB 2 and EPUB 3 | PDF |
|---|---|---|
| Visible rendering | Sanitized HTML, embedded styles, fonts, tables, and images | High-resolution rendering of the original page |
| Contents navigation | EPUB 3 navigation documents and EPUB 2 NCX | Embedded PDF outline; page fallback when no outline exists |
| Text selection | Native chapter text | Position-aligned transparent word layer |
| Single-word definitions | Yes | Yes, when embedded text or OCR is available |
| Complete-phrase definitions | Yes | Yes, with PDF words reconstructed in reading order and separated correctly |
| Full-book search | Chapter text | Extracted or OCR-recovered page text |
| Notes and exact-position marks | Chapter plus within-chapter position | Page plus within-page position |
| Appearance controls | Night, Paper, Sepia, and adjustable type size | Original page appearance; theme changes the surrounding reader canvas |
| Password protection | Not applicable | Secure password prompt; password is not persisted |

## Major capabilities

- A searchable home shelf for every .epub and .pdf file in the launch directory.
- Live shelf filtering by title, author, or filename.
- File-dialog, command-line, and drag-and-drop opening.
- Nested contents, in-book search with excerpts and match counts, previous/next navigation, and overall progress.
- Automatic restoration of the last section or PDF page and the exact within-page scroll position.
- Pixel-precision two-finger touchpad scrolling throughout the shelf, reader, sidebars, lists, dialogs, and definition cards.
- Night, Paper, and Sepia application themes.
- Responsive EPUB typography from 14 through 32 pixels.
- Faithful PDF page rendering with colors, vector graphics, images, rotations, and landscape pages preserved.
- Password-protected PDF support.
- Optional Tesseract OCR fallback for image-only PDF pages.
- Single-word and complete-phrase definitions with append-only source cards.
- Offline WordNet definitions, several conventional online sources, contextual morphology, an optional Tlamatini Googler, and an optional Ollama model.
- A 20-second animated multi-source lookup session with retries and a useful result as soon as any source succeeds.
- Portable, searchable notes and reading marks spanning all EPUB and PDF books.
- Strict link activation: book links do nothing unless Ctrl is held.
- Secure EPUB extraction, active-content removal, restrictive content security policies, and protected navigation.
- An original multi-resolution Windows icon and a source-based desktop shortcut workflow.

## Installation

Python 3.10 or newer is required. From the repository directory:

~~~powershell
python -m pip install -r requirements.txt
python run_reader.py
~~~

For an editable installation and the lumen-reader command:

~~~powershell
python -m pip install -e .
lumen-reader
~~~

The runtime dependencies are PySide6, Beautiful Soup, NLTK, and PyMuPDF. Test-only dependencies are available through the test extra.

## Opening books

Use any of these methods:

1. Select **Open a Book** on the shelf or **Open Book** in the header.
2. Press Ctrl+O.
3. Drop an EPUB or PDF anywhere on the Lumen window.
4. Double-click a title on the shelf.
5. Supply a document path on the command line.

~~~powershell
python run_reader.py "AI Superpowers_ China, Silicon Valley, and the New World Order by Kai-Fu Lee.epub"
python run_reader.py "My Document.pdf"
~~~

Lumen scans only the directory from which it is launched. Launch it from the folder containing the desired library, or open a document directly. The application also retains up to eight recently opened books even when they are outside that launch directory.

## Optional Windows desktop shortcut

The repository includes lumen_reader/assets/lumen.ico. The following PowerShell snippet creates a desktop shortcut that launches Lumen without a console window and uses that icon:

~~~powershell
$repo = (Get-Location).Path
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "Lumen Book Reader.lnk"))
$shortcut.TargetPath = (Get-Command pythonw.exe).Source
$shortcut.Arguments = '"' + (Join-Path $repo "run_reader.py") + '"'
$shortcut.WorkingDirectory = $repo
$shortcut.IconLocation = (Join-Path $repo "lumen_reader\assets\lumen.ico") + ",0"
$shortcut.Save()
~~~

If Windows still shows a cached Python icon, delete the old shortcut and create it again with a new name, or restart Windows Explorer so the icon cache is refreshed.

## The reading interface

### Library view

The home page lists supported books from the working directory together with recent books. The search box filters title, author, and filename simultaneously. Two-finger scrolling works over cards, labels, blank areas, and the shelf itself.

### Reader header

- **☰** shows or hides the book panel.
- **← My Library** saves the current location before returning to the shelf.
- **Search from here** finds the next occurrence from the open page. Use the attached menu to search only the current page or the entire book, moving forward or backward. Press Enter repeatedly to advance.
- **A−** and **A+** adjust EPUB type size. They are disabled for PDF because a PDF retains its original typography.
- **Night / Paper / Sepia** changes the application and EPUB reading theme. For PDF, it changes the surrounding canvas without recoloring the document.
- **⚡ Speed** opens the configurable fullscreen RSVP reader for EPUB and text-bearing PDF books. It starts at the current location; press Space to pause and Esc to return. See [SpeedReadingToolInLumenReader.md](SpeedReadingToolInLumenReader.md).
- **◇ Definer** configures contextual analysis, Tlamatini Googler, and Ollama.
- **Notes & Marks** opens the cross-book index.
- **✦ Mark position** stores the exact reading location and an optional note.
- **Open Book** opens another EPUB or PDF.

### Side panel

The side panel contains:

- **Contents** — nested EPUB navigation or a PDF outline/page list.
- **Search** — case-insensitive full-document search with excerpts and per-section match counts.
- **Notes** — marks belonging to the current book, each reopening its stored position.

### Footer and progress

Previous and Next move between EPUB sections or PDF pages. The slider moves within the current section or page. The percentage beside it represents progress through the complete book, not merely the visible page.

Progress is polled while reading and saved periodically, when changing sections, when returning to the library, and when closing the application.

## Definition mechanics

Lumen defines expressions; it does not translate them.

### Selecting text

| Gesture | Result |
|---|---|
| Double-click one word | Starts its definition immediately |
| Drag-select one word | After one second, shows the clickable 📖 ❔ Define Selected Word prompt |
| Drag-select two or more words | After two seconds, shows the clickable 📖 ❔ Define Selected Phrase prompt |
| Double-click inside an existing phrase selection | Does not replace the phrase with the clicked word |

Multi-word selections are intentionally started only through the selection prompt. This protects a complete phrase such as “Laughing Out Loud” from collapsing to “Out” when the user clicks it.

Selections are normalized before lookup. One word may contain apostrophes or hyphens. A phrase may contain up to 24 words and 180 characters.

### PDF selection reconstruction

PDF words are independent positioned elements rather than ordinary flowing HTML. Chromium can therefore return a visually selected phrase without spaces. Lumen reconstructs the selection from the intersected PDF word elements in document order and inserts one space between them before definition or note capture.

For example:

~~~text
fundamental units of mathematics
~~~

is sent exactly as shown, never as fundamentalunitsofmathematics. The reconstruction works in forward or backward selections and across PDF lines.

### Append-only definition card

Every distinct incoming definition becomes a separate immutable source card. A later result never replaces text the reader is already reading. Exact duplicate definitions are suppressed, while genuinely different senses remain visible.

Cards may include:

- Source and lookup headword.
- Pronunciation.
- Part of speech.
- One or more definitions.
- A short contextual example.
- Up to six synonyms.

The animated four-frame chronometer reports remaining time, definitions already available, and outstanding sources. If at least one result arrives, it is shown immediately while additional sources continue. Lumen reports a total failure only after the complete 20-second session ends without a usable definition.

### Definition source ladder

~~~mermaid
flowchart TD
    S["Selected word or complete phrase"] --> C["Persistent successful-result cache"]
    S --> W["Bundled WordNet — offline"]
    S --> K["Wiktionary — online"]
    S --> T{"Single word or phrase?"}
    T -->|Single word| D["DictionaryAPI.dev"]
    T -->|Phrase| P["Wikipedia phrase context"]
    T -->|Phrase| M["Datamuse interpretation"]
    W --> A["Append distinct source cards"]
    K --> A
    D --> A
    P --> A
    M --> A
    S --> X{"No conventional result after a short head start?"}
    X --> I["Transparent morphology and compound inference"]
    X --> G["Optional Tlamatini Googler"]
    X --> O["Optional Ollama contextual definer"]
    I --> A
    G --> A
    O --> A
~~~

The conventional sources run concurrently:

- **Princeton WordNet 3.0** — bundled and offline; includes lemmatization for many inflected forms.
- **DictionaryAPI.dev** — online single-word senses, pronunciations, examples, and synonyms.
- **English Wiktionary** — structured online word or phrase senses.
- **English Wikipedia** — concise exact-title or redirect context for phrases.
- **Datamuse** — phrase relations accepted only when the returned headword or definition contains verifiable evidence for the complete selection; broad semantic neighbors are discarded.

Network failures are retried at staggered delays while time remains. Successful conventional results are cached atomically in dictionary-cache.json so they can be reused without waiting for the same service.

### Deep Definition expert sources

Select **◇ Definer** to configure the fallback ladder:

1. **Contextual morphology and compound analysis** is enabled by default. It runs locally, explains plausible compounds or productive wordplay, and always labels inferences as inferred.
2. **Tlamatini Googler** is enabled automatically only when C:\Tlamatini\agents\googler\googler.py exists. Its directory is editable. Lumen runs it in an isolated subprocess with a bounded timeout so the agent cannot alter the reader process.
3. **Ollama** is disabled by default. When enabled, Lumen discovers models from the configured host, accepts editable local or cloud model tags such as glm-5.2:cloud, and requests validated JSON from a zero-temperature lexicographer prompt.

Ollama output is validated before rendering. Arbitrary model text, malformed JSON, empty definitions, oversized fields, and excessive synonyms are rejected or bounded.

#### Enable LLM definitions with Ollama — four steps

Ollama is required only if you want Lumen to use an LLM for difficult definitions such as coined terms, technical expressions, contextual meanings, and compound words. The normal WordNet and online-dictionary pipeline works without Ollama.

##### 1 · Install Ollama

Download and install [Ollama for Windows](https://ollama.com/download/windows). The normal Windows installer starts Ollama in the background, adds the ollama command to the terminal, and exposes its local API at http://127.0.0.1:11434.

Open a new PowerShell window after installation and confirm that the command is available:

~~~powershell
ollama --version
ollama ls
~~~

If Lumen is already open, restart it after installing Ollama.

##### 2 · Choose local models or optional Ollama Cloud

You have two valid configurations:

- **Local model — free and private:** the model runs on your own CPU/GPU. No account, token, or subscription is required. Download size, memory use, and response speed depend on the model.
- **Ollama Cloud — optional:** larger hosted models run without requiring a powerful local GPU. Sign in with an Ollama account:

~~~powershell
ollama signin
~~~

Ollama currently offers some cloud access on its Free plan. Ollama Pro is optional and is intended for larger models, higher usage, and more concurrent cloud work; it is not required to enable LLM definitions in Lumen. Current plans and prices are controlled by Ollama and should be checked at [ollama.com/pricing](https://ollama.com/pricing).

When a cloud model is selected, the expression and its short book context leave the local computer through Ollama Cloud. Local models keep that request on the machine.

##### 3 · Pull one definition model

Lumen uses one Ollama chat model at a time. Pull at least one model before opening **◇ Definer**.

For the cloud model used as Lumen’s default example:

~~~powershell
ollama pull glm-5.2:cloud
~~~

For a smaller model that runs locally, choose one appropriate for the computer:

~~~powershell
ollama pull llama3.2:3b
~~~

or:

~~~powershell
ollama pull qwen3:4b
~~~

The local examples are relatively compact instruction models, but their actual memory and disk requirements still vary. The [Ollama model library](https://ollama.com/search) provides current sizes, tags, capabilities, and model-specific licenses.

Test the selected model once before using it in Lumen:

~~~powershell
ollama run llama3.2:3b
~~~

Enter a short prompt, then use /bye to leave the model. Running it once also warms a local model so it is more likely to respond inside Lumen’s bounded 20-second definition window.

Confirm the exact installed tag:

~~~powershell
ollama ls
~~~

The name configured in Lumen must exactly match a tag shown by that command, including :cloud, :3b, or :4b when present.

##### 4 · Connect Lumen to Ollama

1. Start Lumen and select **◇ Definer**.
2. Check **Enable Ollama only after conventional sources miss**.
3. Set **Ollama host** to http://127.0.0.1:11434 for the normal local installation.
4. Select **Discover models**.
5. Choose the exact model tag pulled above, or type it into the editable model box.
6. Select **Save definition sources**.

No token is needed when Lumen connects to the local Ollama API. For a :cloud model, the local Ollama installation handles the account authentication established by ollama signin; Lumen does not store an Ollama password, API key, or account token.

Ollama is an expert fallback rather than the first lookup. Lumen gives WordNet and its conventional dictionary sources a short head start. Only when they have not produced a definition does Lumen send the selected expression, a bounded surrounding passage, and the current book/section title to the configured model.

If **Discover models** reports that Ollama is offline:

- Confirm Ollama is running in the Windows system tray.
- Run ollama ls in a new terminal.
- Keep the host at http://127.0.0.1:11434 unless Ollama deliberately runs elsewhere.
- If necessary, run ollama serve, reopen **◇ Definer**, and select **Discover models** again.
- If a model is absent, pull it first and then refresh the model list.

### Context and privacy

- WordNet and local morphology do not require a network connection.
- Conventional online services receive the selected expression only.
- Tlamatini Googler receives an exact definition-oriented web query.
- Ollama receives the selected expression, up to a short surrounding passage, and the current book and chapter/page titles for contextual disambiguation.
- Ollama is opt-in. Lumen stores its host and model selection, but it does not store an Ollama password or API key.
- Source content is labeled in every appended card.

Availability, privacy policy, rate limits, and terms for online services remain controlled by their providers.

## PDF implementation

Each PDF page has two synchronized layers:

1. PyMuPDF renders the original page to a high-resolution RGB image, including colors, graphics, images, rotation, and annotation appearance.
2. PyMuPDF word coordinates create a transparent selectable layer aligned over that image.

The reader measures and horizontally fits each invisible word to its original bounding box. The page image and text layer then scale together as the window changes size. This preserves visual fidelity while allowing native selection, dictionary context, search, quotations, and notes.

PDF metadata supplies title, author, subject, producer, and identifiers when available. The first page becomes the sidebar preview. Embedded outlines become nested contents entries. When no outline exists, Lumen supplies page navigation; long documents are grouped into manageable page ranges.

Password-protected documents display a masked password prompt. An incorrect password can be retried. The password is held only long enough to open the current document and is not written to reader-state.json.

### Scanned PDFs and OCR

Image-only pages render even when no text exists. Definitions and text search require either:

- An embedded PDF text layer, or
- Tesseract available as a command on PATH.

When Tesseract is found, Lumen asks PyMuPDF for English OCR at 150 DPI on pages that have no extracted words. Restart Lumen after installing Tesseract. If neither embedded text nor OCR yields words, Lumen shows a clear “Definitions unavailable on this page” notice while leaving the visual page readable.

### Intentional PDF limitations

- PDF typography is fixed; EPUB font controls do not modify it.
- Themes do not recolor document pixels.
- Interactive forms, embedded JavaScript, audio/video, and active PDF widgets are not executed.
- Annotation appearances are rendered visually, but interactive PDF annotation behavior is not reproduced.
- Search and definitions depend on extraction/OCR quality and reading order.
- Handwriting, mathematical notation, unusual encodings, or complex multi-column layouts may not produce perfect selectable text.

## EPUB implementation and safety

Lumen parses META-INF/container.xml, the package document, metadata, manifest, spine, cover information, EPUB 3 navigation, and EPUB 2 NCX.

Before rendering an EPUB, Lumen:

- Rejects files that are not valid ZIP archives.
- Rejects absolute paths, parent traversal, drive-like archive paths, and any extraction target outside its temporary directory.
- Rejects archives larger than 512 MiB when uncompressed.
- Removes scripts, iframes, embedded objects, forms, input controls, buttons, base elements, and other active content.
- Removes event-handler attributes, srcdoc, and form-action attributes.
- Allows only safe local stylesheets and book assets.
- Applies a restrictive content security policy that blocks remote book content.
- Places every hyperlink outside the Tab focus chain.

EPUB content is rendered from a private temporary extraction directory that is removed when the book closes.

## Link and focus policy

Ordinary clicks on links are inert so selection cannot unexpectedly jump thousands of lines or navigate away from the reading position. Links are also non-tabbable, and Tab/Shift+Tab events inside the reading surface are intercepted as an additional safeguard.

Hold Ctrl while clicking to activate a link:

- HTTP, HTTPS, and mail links open through the operating system’s default application.
- Internal EPUB links remain inside Lumen and open the target section deliberately.
- PDF pages currently expose annotation appearance but not interactive link hotspots.

Focused anchors are blurred and the pointer-down scroll position is restored to prevent Chromium fragment-focus jumps.

## Notes and reading marks

Press Ctrl+B or select **✦ Mark position** to store:

- Absolute book path, title, and author.
- EPUB section or PDF page.
- Within-section scroll percentage and overall-book percentage.
- Optional comment.
- Optional selected quotation, up to 1,000 characters.
- Up to 20 unique, case-insensitive tags.
- Created and updated timestamps.

PDF quotation capture uses the same word-order and space reconstruction as PDF definitions.

The **Notes & Marks** manager searches book titles, authors, filenames, sections/pages, comments, quotations, and tags. It can reopen a mark from outside the book, edit its comment/tags, delete it after confirmation, and open the data folder.

Marks are written atomically to lumen-reading-marks.json in the launch directory. This makes the notes index portable with the book library and easy to inspect or back up. Legacy bookmarks from reader-state.json are migrated when their book is next opened.

## Persistence and generated data

| Data | Location | Contents |
|---|---|---|
| Reader state | Operating system application-data location / reader-state.json | Theme, EPUB font size, sidebar visibility, recent books, per-book section/page, scroll position, and definer settings |
| Dictionary cache | Beside reader-state.json as dictionary-cache.json | Up to 2,500 successful conventional definitions |
| Notes and marks | Launch directory / lumen-reading-marks.json | Portable cross-book positions, notes, quotes, and tags |
| EPUB extraction | Private temporary directory | Sanitized source assets; deleted when the book closes |
| PDF render cache | Private temporary directory | Rendered pages and selection data; deleted when the document closes |

State and mark writes use a temporary file followed by replacement to reduce the risk of partial JSON files.

## Keyboard and pointer controls

| Action | Control |
|---|---|
| Open a book | Ctrl+O or drag and drop |
| Return to the library | Alt+Left or **← My Library** |
| Search inside the current book | Ctrl+F |
| Mark the current position / add a note | Ctrl+B |
| Search notes and marks from every book | Ctrl+Shift+M |
| Previous section or PDF page | Ctrl+Left |
| Next section or PDF page | Ctrl+Right |
| Decrease EPUB type size | Ctrl+- |
| Increase EPUB type size | Ctrl++ |
| Reset EPUB type size | Ctrl+0 |
| Close a definition or selection prompt | Escape |
| Activate a book hyperlink | Ctrl+click |
| Scroll | Mouse wheel, precision touchpad, touchscreen kinetic gesture, or scrollbars |

## Architecture

~~~mermaid
flowchart LR
    A["app.py — bootstrap and storage locations"] --> U["ui.py — Qt interface and interaction policy"]
    U --> E["book.py — safe EPUB adapter"]
    U --> P["pdf_book.py — faithful PDF adapter"]
    E --> V["Protected Qt WebEngine reading surface"]
    P --> V
    V --> D["dictionary.py — conventional sources and cache"]
    D --> S["smart_definition.py — contextual and expert fallbacks"]
    U --> M["marks.py — portable cross-book notes"]
    U --> R["storage.py — preferences and progress"]
~~~

The EPUB and PDF adapters intentionally expose the same page-oriented interface: metadata, chapters/pages, contents entries, rendered HTML, searchable text, URL mapping, cover preview, stable book key, and cleanup. This keeps navigation, definitions, notes, and persistence format-independent.

## Project layout

- lumen_reader/app.py — QApplication bootstrap, icons, storage locations, launch-directory discovery, and command-line opening.
- lumen_reader/book.py — secure EPUB extraction, metadata, spine/navigation parsing, sanitization, rendering, and search.
- lumen_reader/pdf_book.py — PDF metadata, password handling, outlines, rendering, positioned text, OCR fallback, and search.
- lumen_reader/ui.py — shelf, reader, selection reconstruction, definitions, notes dialogs, navigation policy, themes, scrolling, and shortcuts.
- lumen_reader/dictionary.py — normalization, cache, WordNet, DictionaryAPI, Wiktionary, Wikipedia, and Datamuse parsing.
- lumen_reader/smart_definition.py — contextual morphology, isolated Tlamatini execution, Ollama payload validation, and expert-result parsing.
- lumen_reader/marks.py — atomic portable cross-book notes and exact-position marks.
- lumen_reader/storage.py — atomic preferences, recents, and per-book progress.
- lumen_reader/models.py — shared metadata, chapter, contents, search-result, and legacy-bookmark models.
- lumen_reader/assets — original application icons and the bundled WordNet archive.
- tests — EPUB/PDF integration, security, persistence, dictionary, marks, shelf, link-policy, and smart-definition regressions.
- run_reader.py — direct development entry point.
- THIRD_PARTY_NOTICES.md — runtime, data-source, optional-service, and test-tool notices.

## Testing

Install the test extra and run the complete suite:

~~~powershell
python -m pip install -e ".[test]"
python -m pytest
~~~

The current suite contains 45 passing tests. Coverage includes:

- Complete parsing and rendering of the repository EPUB fixtures.
- EPUB metadata, cover, navigation, local links, case-insensitive search, sanitization, and ZIP path-traversal rejection.
- Generated multi-page PDF metadata, original-color rendering, outlines, landscape and rotated pages, encrypted documents, and URL/page mapping.
- PDF word coordinates, phrase context, search, and saved page/within-page position.
- A live Qt WebEngine regression proving that a raw concatenated PDF selection is reconstructed with spaces before definition.
- Word and phrase normalization, one- and two-second prompt timing, definition parsers, append-only sources, and deep-definition validation.
- Atomic reader storage, dictionary caching, cross-book marks, link activation policy, and shelf filtering.

The generated PDF fixture exists only during a test and does not add third-party document content to the repository.

## Troubleshooting

### A PDF is visible but cannot be selected or defined

The page is probably image-only or its embedded text is unusable. Install Tesseract with the English language data, make sure the tesseract command is on PATH, restart Lumen, and reopen the PDF.

### A selected PDF phrase has missing spaces

Current Lumen reconstructs PDF words before every definition and note capture. Ensure the running process was restarted after updating the source. The regression suite verifies this behavior through Qt WebEngine.

### A definition remains pending

Offline WordNet may still succeed. Online sources retry only while the 20-second session has time remaining. Check the network, then use the retry button. Optional expert sources can be enabled and tested under **◇ Definer**.

### Ollama says it is offline

Start Ollama, verify the configured host, and select **Discover models**. Lumen accepts either the host root or an address ending in /api and normalizes the API endpoints automatically.

### A book does not appear on the shelf

Confirm that its suffix is .epub or .pdf and that it is in Lumen’s launch directory. You can still use Ctrl+O, drag-and-drop, or a direct command-line path.

### A normal link click does nothing

This is intentional. Hold Ctrl while clicking. The policy prevents focus-driven anchor jumps during text selection.

### The desktop shortcut shows the Python icon

Confirm that its icon points to lumen_reader/assets/lumen.ico, recreate the shortcut if necessary, and refresh Windows Explorer’s icon cache.

## Licensing and external services

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for runtime dependency licenses, bundled lexical data, optional OCR/AI components, online definition providers, test-only tools, and upstream project links.

Lumen’s application icon and branding assets are original project artwork and are not based on an existing trademark.
