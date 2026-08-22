# Third-party notices

This document identifies the principal third-party runtime libraries, bundled lexical data, optional external components, online definition services, and test-only tools used by Lumen Book Reader 1.3.0.

It is an attribution and dependency inventory, not a replacement for the complete upstream license texts or legal advice. Anyone redistributing Lumen or a packaged executable must review the exact dependency versions and satisfy every applicable license.

## Runtime software

### PySide6 and Qt

Lumen uses PySide6 for the native interface and the Qt modules used for widgets, networking, image handling, kinetic scrolling, desktop integration, and Qt WebEngine.

PySide6 package metadata describes its licensing as LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only. Qt is also available under commercial terms. Individual Qt modules and bundled third-party components may have additional terms.

- PySide project: <https://doc.qt.io/qtforpython-6/>
- Qt licensing overview: <https://doc.qt.io/qt-6/licensing.html>
- Qt third-party code and software bill of materials: <https://doc.qt.io/qt-6/licenses-used-in-qt.html>

Qt WebEngine incorporates Chromium and its third-party components. Distributors should retain the notices delivered with the selected Qt/PySide6 binaries.

### PyMuPDF and MuPDF

Lumen uses PyMuPDF to:

- Open and authenticate PDF documents.
- Read metadata and nested outlines.
- Render original PDF pages, colors, images, vector graphics, rotations, and annotation appearances.
- Extract position-aware word bounding boxes.
- Create the transparent selectable layer used for definitions, phrase reconstruction, search, quotations, and notes.
- Invoke optional Tesseract OCR for image-only pages.

PyMuPDF and MuPDF are dual-licensed under the GNU Affero General Public License version 3 and commercial terms from Artifex. This dependency deserves particular attention when Lumen is redistributed or incorporated into another product.

- PyMuPDF licensing and copyright: <https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright>
- GNU AGPL version 3: <https://www.gnu.org/licenses/agpl-3.0.html>
- Artifex licensing: <https://artifex.com/licensing/>

### Beautiful Soup

Lumen uses Beautiful Soup to parse, sanitize, and restyle EPUB HTML and to reduce structured dictionary markup to safe plain text.

Beautiful Soup is distributed under the MIT License.

- Project and documentation: <https://www.crummy.com/software/BeautifulSoup/bs4/doc/>
- Source repository: <https://code.launchpad.net/beautifulsoup>

### NLTK

Lumen uses NLTK as the Python interface to the bundled Princeton WordNet corpus and for WordNet lemmatization.

NLTK is distributed under the Apache License 2.0.

- Project: <https://www.nltk.org/>
- Source and license: <https://github.com/nltk/nltk>
- Apache License 2.0: <https://www.apache.org/licenses/LICENSE-2.0>

## Bundled lexical data

### Princeton WordNet 3.0

Lumen includes the Princeton WordNet 3.0 lexical database for reliable offline English definitions, separate senses, synonyms, examples, and inflected-form recovery.

Copyright 2006 by Princeton University. All rights reserved. Permission to use, copy, modify, and distribute the database and its documentation for any purpose and without fee or royalty is granted under the WordNet license.

The complete license is preserved inside lumen_reader/assets/nltk_data/corpora/wordnet.zip as wordnet/LICENSE.

- Official license and commercial-use information: <https://wordnet.princeton.edu/license-and-commercial-use>
- WordNet project: <https://wordnet.princeton.edu/>

## Optional local or user-installed components

These components are not bundled by Lumen and are used only when the user installs or enables them.

### Tesseract OCR

When the tesseract command is available on PATH, PyMuPDF can recover selectable English text from PDF pages that contain no embedded words. The visible PDF page still renders without Tesseract; OCR affects selection, definitions, quotations, and search.

Tesseract is distributed under the Apache License 2.0. Trained-language data may carry its own notices.

- Project: <https://github.com/tesseract-ocr/tesseract>
- License: <https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE>
- Language data: <https://github.com/tesseract-ocr/tessdata>

### Ollama and selected models

Ollama is an optional, disabled-by-default contextual definition provider. Lumen contacts a user-configured Ollama host, discovers its available models, and sends a bounded lexicographer request only after conventional sources miss.

Ollama itself is not bundled. Ollama software and every selected model may have different licenses, acceptable-use conditions, privacy behavior, hosting arrangements, and data-retention policies. A cloud-tagged model may transmit the selected expression and captured passage beyond the local computer.

- Ollama project: <https://github.com/ollama/ollama>
- Ollama API documentation: <https://github.com/ollama/ollama/blob/main/docs/api.md>
- Ollama model library: <https://ollama.com/search>

Users and distributors are responsible for reviewing the terms of the particular model configured in **◇ Definer**.

### Tlamatini Googler and Playwright

Lumen can optionally invoke a separate Tlamatini Googler installation, normally located at C:\Tlamatini\agents\googler. The agent is not part of this repository. Lumen launches it in an isolated subprocess with a bounded timeout and parses only labeled web evidence.

The external agent may use Playwright and a Chromium browser to consult Google or DuckDuckGo. Those components and services are governed by their own licenses and terms.

- Playwright: <https://playwright.dev/>
- Playwright source and Apache-2.0 license: <https://github.com/microsoft/playwright>
- Google terms: <https://policies.google.com/terms>
- DuckDuckGo terms: <https://duckduckgo.com/terms>

The local Tlamatini installation should carry its own notices for its code and dependencies.

## Online definition and context services

Lumen labels every accepted online contribution in the append-only definition card. Service availability, rate limits, content licensing, privacy policies, and API terms are controlled by the providers and may change independently of Lumen.

### DictionaryAPI.dev

DictionaryAPI.dev supplies online single-word pronunciations, parts of speech, definitions, examples, and synonyms.

- API project: <https://dictionaryapi.dev/>
- API endpoint used by Lumen: <https://api.dictionaryapi.dev/api/v2/entries/en/>

Lumen sends only the normalized single-word expression.

### English Wiktionary

Lumen queries the structured English Wiktionary definition endpoint for both words and complete phrases. Displayed cards are labeled “Wiktionary · online.”

Wiktionary text is generally available under Creative Commons Attribution-ShareAlike and, for qualifying contributions, the GNU Free Documentation License. Consult Wikimedia for exact reuse conditions and attribution requirements.

- English Wiktionary: <https://en.wiktionary.org/>
- Wikimedia REST API: <https://www.mediawiki.org/wiki/Wikimedia_REST_API>
- Wikimedia Foundation terms of use: <https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use>
- Wikimedia reuse guidance: <https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use/Frequently_asked_questions>

### English Wikipedia

Lumen requests a concise, plain-text introductory extract for an exact multi-word phrase or redirect. It rejects missing and disambiguation-style results and labels accepted text “Wikipedia phrase context · online.”

Wikipedia text is generally available under Creative Commons Attribution-ShareAlike, with additional historical licensing considerations described by Wikimedia.

- English Wikipedia: <https://en.wikipedia.org/>
- MediaWiki API: <https://www.mediawiki.org/wiki/API:Main_page>
- Wikimedia Foundation terms of use: <https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use>

### Datamuse API

Lumen uses Datamuse to obtain a clearly labeled related-expression interpretation for complete phrases when exact dictionary entries are unavailable. Datamuse definitions can draw on sources such as Wiktionary and WordNet.

- API documentation: <https://www.datamuse.com/api/>
- Datamuse: <https://www.datamuse.com/>

Lumen sends only the normalized complete phrase.

### Web evidence from optional Googler

If Tlamatini Googler is enabled, it performs a definition-oriented exact search and can return short evidence associated with an original result URL. Copyright in pages, snippets, and descriptions remains with the respective publishers. Search-engine and publisher terms apply.

## Test-only dependencies

The following packages are included in the optional test dependency group and are not required to run Lumen:

### pytest

pytest executes the regression suite, including EPUB parsing, PDF rendering, live Qt WebEngine phrase reconstruction, dictionaries, notes, persistence, and security policy tests.

pytest is distributed under the MIT License.

- Project and license: <https://github.com/pytest-dev/pytest>

### pypdf

pypdf creates encrypted and rotated PDF variants for regression testing.

pypdf is distributed under the BSD 3-Clause License.

- Project and license: <https://github.com/py-pdf/pypdf>

### ReportLab

ReportLab generates the temporary multi-page PDF fixture used to test metadata, outlines, original colors, vector shapes, landscape pages, text positioning, and phrase definitions.

ReportLab is distributed under a BSD-style license.

- Project: <https://www.reportlab.com/opensource/>
- Source and license: <https://github.com/MrBitBucket/reportlab-mirror>

## Project-created assets and generated data

The Lumen open-book icon in lumen_reader/assets is original project artwork and is not copied from an existing trademark or third-party icon set.

Reader state, dictionary caches, reading marks, rendered temporary PDF pages, extracted EPUB files, screenshots, and generated test fixtures are application or user data rather than bundled third-party libraries. Copyright and other rights in books opened by the user remain with their respective owners.

Lumen does not grant permission to redistribute EPUBs, PDFs, dictionary content, web excerpts, model outputs, or other user-supplied material beyond the permissions supplied by their owners and applicable licenses.

## Redistribution checklist

Before distributing Lumen or a packaged executable:

1. Determine and document the licensing basis for PyMuPDF/MuPDF.
2. Select and comply with the appropriate Qt/PySide6 licensing option.
3. Retain licenses and copyright notices shipped with Python wheels, Qt, Qt WebEngine, Chromium, MuPDF, NLTK, Beautiful Soup, and WordNet.
4. Preserve visible source attribution for dictionary and Wikimedia contributions.
5. Keep optional Tesseract, Ollama models, Tlamatini, Playwright, and web services clearly distinguished from bundled code.
6. Rebuild the dependency inventory from the exact versions being shipped.
7. Review privacy disclosures if Ollama cloud models or external web evidence are enabled by default in a redistributed build.

Upstream names and trademarks belong to their respective owners. Their mention describes interoperability or attribution and does not imply endorsement.
