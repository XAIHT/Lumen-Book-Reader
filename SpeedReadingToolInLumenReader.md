# Speed Reading Tool in Lumen Reader

*Lumen Book Reader 1.1.0 · created by Angela López Mendoza · @angelahack1*

## Executive summary

Lumen includes an optional, format-neutral **Speed Reader Studio**. Select **⚡ Speed** in the reader header (or press **Ctrl+Shift+R**) to configure and launch it. The button exists only while a book is open.

**1.1.0 adds explicit start and end markers.** A session no longer begins at an estimate derived from scroll position: the reader points the cursor at the precise word the stream should open on. When the session ends, Lumen marks the exact final chunk that was actually displayed, so the return to page reading is a known position rather than a search. Both markers are described under [User experience](#user-experience).

The tool uses **rapid serial visual presentation (RSVP)**: a word or short phrase is placed at one stable fixation point, removed, and replaced by the next unit. It works with both EPUB and text-bearing PDF files because both Lumen book adapters already expose the same `text_for_chapter(index)` interface. An EPUB “chapter” is a spine section; a PDF “chapter” is a page.

This implementation is intentionally powerful but scientifically cautious. RSVP can reduce eye movements and can be useful for focus, skimming, small displays, or paced practice. It does **not** remove the cognitive time required for comprehension, and high speed can reduce comprehension or increase visual fatigue. The fluorescent-on-black option is a configurable presentation style, not a claim that rods, retinal afterimages, or “persistence of vision” improve memory.

## What was learned from SwiftRead

The current [SwiftRead product page](https://swiftread.com/) presents a broad reading-optimization toolkit rather than only a word flasher. Its public description emphasizes:

- fixed-location sequential presentation intended to reduce eye movement and regression;
- configurable words per minute (WPM), themes, rhythm, and layout;
- import of webpages, pasted text, PDF, EPUB, Kindle Cloud, and Libby content;
- optional text-to-speech;
- a focus-oriented, low-distraction experience.

Lumen adopts the useful interaction principles—one focal location, configurable WPM, rhythm controls, document import through the reader’s existing adapters, and minimal chrome—but it does not reproduce SwiftRead’s service, branding, proprietary behavior, or marketing claims.

## Evidence and design position

RSVP is a legitimate experimental and interface technique, but “faster display” and “faster understanding” are not interchangeable.

1. A 2018 study comparing normal reading with RSVP at 250–450 WPM reported no significant comprehension difference through roughly 350 WPM in that experiment, with significantly lower scores at higher rates. This supports Lumen’s conservative 300 WPM default and its easy 25-WPM adjustments. See [Di Nocera, Ricciardi, and Juola, DOI 10.1504/IJHFE.2018.10016316](https://doi.org/10.1504/IJHFE.2018.10016316).
2. A study of Spritz-style presentation found impaired literal comprehension and increased visual fatigue relative to traditional reading. See [Benedetto et al., DOI 10.1016/j.chb.2014.12.043](https://doi.org/10.1016/j.chb.2014.12.043).
3. Natural readers regress to earlier words when comprehension needs repair. Preventing that control can harm comprehension. See [Schotter, Tran, and Rayner, DOI 10.1177/0956797614531148](https://doi.org/10.1177/0956797614531148). Lumen therefore provides pause, a ten-second rewind, a seek bar, and immediate return to the normal page.
4. At extreme rates (700–1,000 WPM), static text produced better comprehension than the tested RSVP conditions. See [Acklin and Papesh, DOI 10.5406/amerjpsyc.130.2.0183](https://doi.org/10.5406/amerjpsyc.130.2.0183). Lumen permits experimentation up to 1,200 WPM but does not present that range as comprehension-safe.
5. Laboratory RSVP timing is not always constant. One published protocol used a base duration plus extra milliseconds per character, illustrating why long words deserve additional exposure. See [Modulation of cortical activity during speed reading, PMC4363175](https://pmc.ncbi.nlm.nih.gov/articles/PMC4363175/). Lumen makes the long-word allowance configurable.
6. Visible persistence and retinal afterimages are distinct phenomena, not a single memory buffer. See [Di Lollo, Clark, and Hogben, DOI 10.3758/BF03210418](https://doi.org/10.3758/BF03210418). Reading also targets the high-acuity fovea, whose center is rod-free and cone-dense; see the [NCBI Neuroscience overview of rods and cones](https://www.ncbi.nlm.nih.gov/books/NBK10848/). The implementation therefore makes no rods/retina memory claim.
7. High-contrast, rapidly changing material deserves an accessibility warning. Lumen changes only the relatively small word region rather than flashing the whole screen, provides pause/blank controls, avoids a red default, and documents discomfort stops. The reference safety threshold is the [W3C WCAG “Three Flashes” guidance](https://www.w3.org/WAI/WCAG20/Understanding/three-flashes); selectable colors and live contrast feedback align with [WCAG 2.2 visual-presentation guidance](https://www.w3.org/TR/WCAG22/#visual-presentation).

The resulting position is pragmatic: RSVP is an optional pacing and focus mode. For difficult, technical, literary, or safety-critical material, normal reading and deliberate rereading remain the reference experience.

## User experience

### Launch

1. Open any EPUB or text-bearing PDF.
2. Navigate to the page or section where speed reading should begin.
3. Select **⚡ Speed** or press **Ctrl+Shift+R**.
4. Configure the session in Speed Reader Studio.
5. Select **Choose starting word**.
6. Point at the first word on the page and click.

Settings are saved in Lumen’s existing reader preferences and restored next time.

### The start marker

Confirming the Studio does not begin playback. It arms *targeting mode* on the live reading surface:

- A fixed heads-up prompt reads **POINT TO THE FIRST WORD — Move precisely, then click to launch RSVP · Esc cancels**.
- A reticle follows the cursor and a **START HERE** tag names the word currently under it, so the choice is confirmed before the click, not after.
- Clicking begins the stream at exactly that word. Nothing is estimated from scroll geometry.
- The **⚡ Speed** header button becomes **✕ Cancel** for the duration. <kbd>Esc</kbd> or that button leaves targeting without changing the reader's place, and clears the overlay completely.

Targeting is format-neutral by construction. On a PDF page it resolves the word from the transparent `.pdf-word` selectable layer; on EPUB text it resolves a caret position from the pointer. In both cases it returns a word *index* into the same array the speed reader will play, so the pointer and the stream cannot disagree.

Ordinary click, auxiliary-click, and navigation events are suppressed while targeting is armed, so choosing a starting word can never follow a link, open a definition, or start a selection. If a malformed EPUB puts display-only nodes in the flow and the clicked token does not match the word at the resolved index, Lumen looks for a *unique* copy of that token within eight words either side and uses it. If there is no unique nearby match, the click is ignored rather than resolved to a distant occurrence the reader did not point at.

### During playback

- **Space**: pause/resume once playback begins. During the welcome countdown it is intentionally ignored so a stray key cannot flash the first word early.
- **Left / Right**: move approximately ten seconds backward/forward at the current WPM.
- **Up / Down**: change speed by 25 WPM.
- **Progress slider**: seek anywhere in the complete book.
- **Esc** or **Close**: return to the normal reader.
- **Click the word**: pause/resume.

### The end marker

When the session closes, Lumen reopens the EPUB section or PDF page that contains the **last chunk actually presented** — not the last chunk scheduled — and marks it:

- A red outline is drawn around that exact word or phrase, on the real page, in place.
- The marker is tagged **LAST WORD READ**, or **LAST PHRASE READ** when the chunk held more than one word.
- A phrase that wraps across a line break is outlined as multiple segments rather than one box spanning the gutter.

The marker is transient by design. <kbd>Esc</kbd>, any click, or any scroll dismisses it; it disappears on navigating away, returning to the shelf, or opening another book. It is never written into the book, into `lumen-reading-marks.json`, or into saved reader state — a reading position is a fact worth keeping, but a "you were here a moment ago" hint is not, and a permanent red box would become visual debris. If the word cannot be located on the rendered page, no marker is drawn and the page simply opens; the reading position is still restored.

Live WPM changes are retained for the next session.

## Configuration reference

| Setting | Range/default | Purpose |
|---|---:|---|
| Nominal speed | 80–1,200; **300 WPM** | Base word exposure rate. Adaptive additions make actual elapsed rate lower around difficult boundaries. |
| Words per fixation | 1–5; **1** | Displays short chunks. A chunk never crosses a detected sentence ending or document section/page. |
| Dark interval | 0–40%; **12%** | Fraction of each unit’s time during which the word is removed, creating the requested appear/disappear rhythm. Set to 0 for continuous replacement. |
| Long-word allowance | 0–60 ms; **12 ms** | Adds time for every character beyond eight in the longest word of the unit. |
| Punctuation rhythm | On by default | Enables clause and sentence timing multipliers. |
| Clause pause | 1.00–3.00×; **1.35×** | Slows units ending in comma, colon, or semicolon. |
| Sentence pause | 1.00–4.00×; **1.85×** | Slows units ending in `.`, `?`, `!`, or ellipsis. |
| Countdown | 3–10 s; **3 s** | Gives the eyes time to acquire the fixation location; the transition cannot be skipped accidentally. |
| Eye-rest reminder | 0–60 min; **10 min** | Pauses and asks the reader to look away and blink; 0 disables it. |
| Typeface | Installed system fonts | Lets the user choose a familiar, legible face. |
| Word size | 28–144 pt; **68 pt** | Large central text; unusually long chunks automatically shrink to fit. |
| Background | **#050709** | Default near-black field. Any valid color can be selected. |
| Word color | **#76FFB2** | Default fluorescent mint. A live contrast ratio is shown. |
| Focal color | **#FFD166** | Highlights the stable recognition character and fixation guides. |
| Focal letter | On by default | Keeps one character at the horizontal center to reduce repeated reacquisition. |
| Fixation guides | On by default | Small marks above and below the focal character. |
| Complete screen | On by default | Uses the full display for a distraction-free black field. |
| Minimal view | On by default | Hides title, buttons, and progress while playing; pausing restores them. |

## Timing model

For a unit containing `n` words, Lumen begins with:

```text
base_ms = 60,000 / WPM × n
```

It then adds `long_word_extra_ms` for each character beyond eight in the longest word. If punctuation rhythm is enabled, the resulting duration is multiplied by the clause or sentence factor. Finally, `blank_percent` divides the total into visible and dark stages.

This is intentionally described as **nominal WPM**. A sentence-heavy passage, long technical vocabulary, and multiword chunks change effective elapsed speed. The result is more natural than treating `superconductivity` and `a` as equally difficult visual events.

## Text and position architecture

```mermaid
flowchart LR
    A["Open EPUB or PDF"] --> B["Existing Lumen book adapter"]
    B --> C["text_for_chapter(index)"]
    C --> D["SpeedReadingDocument"]
    D --> E["Unicode whitespace tokenization"]
    E --> T["Cursor targeting · pointer to word index"]
    T --> F["Seekable cross-chapter cursor"]
    F --> G["Sentence-safe chunking"]
    G --> H["Adaptive visible/blank scheduler"]
    H --> I["Fixed focal-point painter"]
    I --> J["Return chapter/page at the last presented chunk"]
    J --> K["Red end marker on the exact word or phrase"]
```

The document stores per-section word arrays and global word offsets. The cursor can move locally, cross empty sections, seek globally, and map a global word index back to a section. The display is custom-painted: the prefix ends immediately before the screen center, the focal character occupies the center, and the suffix begins immediately after it. This is more stable than merely centering every word’s bounding box.

No book content is executed. The speed reader receives plain text from Lumen’s existing sanitized EPUB extraction or PDF text layer. Images, equations represented only as graphics, footnote layout, tables, and typography are necessarily lost in RSVP mode; return to page view whenever spatial structure matters.

## Format behavior and limitations

### EPUB

All readable spine sections are included. HTML styling, images, and embedded scripts are not part of the speed stream. Sentence punctuation and Unicode words are retained from extracted text.

### PDF

Each PDF page is a speed-reader section. PDFs with a text layer work directly. Image-only/scanned PDFs do not contain extractable words; Lumen reports that OCR is required instead of pretending the page is empty content.

### Known tradeoffs

- RSVP removes parafoveal preview and makes natural regression less immediate.
- Reading speed cannot guarantee learning speed, inference quality, or long-term retention.
- The *scroll* on handoff is still proportional, because HTML/PDF scroll geometry and extracted word position are different coordinate systems. The 1.1.0 end marker removes the consequence rather than the cause: the page may settle a line or two away from the mark, but the marker itself is drawn on the exact word, so the reader is never left estimating where the stream stopped.
- Languages without whitespace-delimited words need a language-aware segmenter in a future version.
- Equations, source-code indentation, poetry lineation, tables, charts, and image captions may need normal page reading.
- The current mode is visual only; synchronized text-to-speech is a separate feature rather than an implicit part of RSVP.

## Comfort and responsible use

Start around 200–300 WPM with one word per fixation. Increase in small steps only while meaning remains clear. Use punctuation pauses and the rest reminder for long sessions. Reduce contrast, remove the dark interval, lower WPM, or leave fullscreen if the default fluorescent presentation feels harsh.

Stop immediately for eye pain, persistent afterimages, headache, dizziness, nausea, visual aura, or seizure-like symptoms. Anyone with photosensitive epilepsy, migraine triggered by visual stimulation, palinopsia/visual-snow symptoms, or a relevant eye/neurological condition should seek individualized clinical advice before using rapidly changing high-contrast presentation.

## Implementation inventory

- `lumen_reader/speed_reader.py`
  - validated persisted settings;
  - EPUB/PDF-neutral document and seek cursor;
  - sentence-aware chunking and adaptive timing;
  - live-preview configuration dialog;
  - focal-point painter;
  - immersive player and controls.
- `lumen_reader/ui.py`
  - book-only **⚡ Speed** header entry, which doubles as **✕ Cancel** while targeting;
  - `Ctrl+Shift+R` shortcut;
  - `RSVP_TARGETING_SCRIPT`, `RSVP_TARGET_TAKE_PICK_SCRIPT`, `RSVP_TARGET_STOP_SCRIPT` — the start-marker overlay, its pick handoff, and its teardown;
  - `rsvp_return_highlight_script()` and `RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT` — the end marker and its dismissal;
  - the event filter that routes <kbd>Esc</kbd>, clicks, and wheel movement to whichever marker is live;
  - session launch, text extraction, persistence, and page handoff.
- `lumen_reader/storage.py`
  - default speed-reader preference record.
- `tests/test_speed_reader.py`
  - cursor/chapter boundary, seek, timing, validation, ORP, and contrast tests.
- `tests/test_rsvp_targeting.py`
  - drives both marker scripts inside a real Chromium page: asserts the pointer resolves the exact visible word index, that the picked token matches, that the end marker reports its segment count and correct `LAST WORD READ` / `LAST PHRASE READ` label, and that both overlays remove themselves completely on stop.

## Validation

- Run the complete automated suite with `python -m pytest`.
- The speed-reader suite covers cursor boundaries, seeking, adaptive timing, settings validation, the non-skippable welcome countdown, ORP placement, and contrast.
- The start and end markers are exercised against a live Qt WebEngine page rather than mocked: real geometry, real hit-testing, real overlay teardown.
- Offscreen Qt rendering verified for the settings studio, RSVP player, and reader header.
- Both the normal and minimal-chrome playback states were exercised.
- Real EPUB-spine and PDF-text-layer adapters both build complete speed-reading documents in the test suite.
- The full regression suite also covers EPUB/PDF parsing, search, marks, dictionary behavior, storage, and safety.

## Sensible future extensions

1. Optional language-specific tokenization for CJK, Thai, and other scripts without whitespace boundaries.
2. OCR integration for scanned PDFs, with a visible confidence indicator.
3. Comprehension checkpoints and a personal calibration curve rather than “maximum WPM” gamification.
4. Optional local text-to-speech synchronized to the displayed unit.
5. Structural parsing for headings, lists, code, poetry, and equations.
6. Adaptive pacing based on user-initiated rewinds and pauses—transparent and local, without making unsupported biometric claims.
