"""Qt user interface for the Lumen EPUB and PDF reader."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from PySide6.QtCore import QByteArray, QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QScroller,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .book import EpubBook, EpubError
from .dialog_layout import ScreenFittingDialog, WheelSafeComboBox
from .dictionary import (
    DictionaryCache,
    DictionaryEntry,
    lookup_offline_wordnet_entries,
    normalize_lookup_text,
    parse_datamuse_phrase,
    parse_dictionary_entries,
    parse_wikipedia_phrase,
    parse_wiktionary_entries,
    selection_lookup_delay_ms,
)
from .marks import MARKS_FILENAME, MarksStore, ReadingMark
from .models import Bookmark, SearchResult, TocEntry
from .pdf_book import PdfBook, PdfError, PdfPasswordRequired
from .library_index import DEFAULT_TEXT_BUDGET, LibraryIndex, default_index_path, normalize_root
from .scan_monitor import ScanMonitorDialog
from .settings_dialog import ConfigurationDialog
from .shelf import LibraryShelf, source_path_text
from .turbo_scan import ScanConfig, TurboScanner
from .storage import ReaderStore
from .speed_reader import (
    SpeedReaderDialog,
    SpeedReaderSettings,
    SpeedReaderSettingsDialog,
    SpeedReadingDocument,
)
from .smart_definition import (
    DEFAULT_GOOGLER_PATH,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    build_ollama_chat_payload,
    default_definition_fallbacks,
    infer_contextual_entries,
    normalized_ollama_url,
    parse_ollama_chat_response,
    run_tlamatini_googler,
)


THEME_NAMES = {"Night": "dark", "Paper": "light", "Sepia": "sepia"}
THEME_LABELS = {value: key for key, value in THEME_NAMES.items()}
DICTIONARY_SESSION_SECONDS = 20.0
DICTIONARY_RETRY_DELAYS_MS = (650, 1400, 2600)
DICTIONARY_API_BASE_URL = "https://api.dictionaryapi.dev/api/v2/entries/en"
WIKTIONARY_API_BASE_URL = "https://en.wiktionary.org/api/rest_v1/page/definition"
WIKIPEDIA_API_BASE_URL = "https://en.wikipedia.org/w/api.php"
DATAMUSE_API_BASE_URL = "https://api.datamuse.com/words"
SUPPORTED_BOOK_SUFFIXES = {".epub", ".pdf"}


def is_supported_book(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_BOOK_SUFFIXES


def library_books(directory: Path) -> list[Path]:
    try:
        return sorted(
            (path for path in directory.iterdir() if path.is_file() and is_supported_book(path)),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return []


def generic_document_label(path: str | Path) -> str:
    return "PDF document" if Path(path).suffix.lower() == ".pdf" else "EPUB book"


def search_results_from_page(
    results: list[SearchResult], start_index: int, backward: bool = False
) -> list[SearchResult]:
    """Order full-book hits from the open page, wrapping once in the chosen direction."""
    if backward:
        return sorted(
            results,
            key=lambda result: (result.chapter_index > start_index, -result.chapter_index),
        )
    return sorted(
        results,
        key=lambda result: (result.chapter_index < start_index, result.chapter_index),
    )


# Installed with runJavaScript after every chapter/page load. EPUB scripts are
# deliberately stripped, so this trusted reader-owned guard remains separate
# from book content and cannot be replaced by an EPUB.
READER_INTERACTION_GUARD_SCRIPT = r"""
(() => {
  if (window.__lumenInteractionGuardInstalled) return true;
  window.__lumenInteractionGuardInstalled = true;

  let pointerScrollX = window.scrollX;
  let pointerScrollY = window.scrollY;
  let linkPointerAt = -Infinity;

  const linkFor = (target) =>
    target && target.closest ? target.closest('a[href]') : null;
  const pdfWordFor = (node) => {
    if (!node) return null;
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element && element.closest ? element.closest('.pdf-word') : null;
  };
  window.__lumenSelectedText = () => {
    const selection = window.getSelection ? window.getSelection() : null;
    if (!selection || !selection.rangeCount) return '';
    const nativeText = selection.toString();
    const allWords = [...document.querySelectorAll('.pdf-word')];
    if (!allWords.length) return nativeText;

    const anchorWord = pdfWordFor(selection.anchorNode);
    const focusWord = pdfWordFor(selection.focusNode);
    let selectedWords = [];
    if (anchorWord && focusWord) {
      const anchorIndex = allWords.indexOf(anchorWord);
      const focusIndex = allWords.indexOf(focusWord);
      if (anchorIndex >= 0 && focusIndex >= 0) {
        const first = Math.min(anchorIndex, focusIndex);
        const last = Math.max(anchorIndex, focusIndex);
        selectedWords = allWords.slice(first, last + 1);
      }
    }
    if (selectedWords.length < 2) {
      const range = selection.getRangeAt(0);
      selectedWords = allWords.filter((word) => {
        try {
          return range.intersectsNode(word);
        } catch (_error) {
          return false;
        }
      });
    }
    if (selectedWords.length < 2) return nativeText;
    return selectedWords
      .map((word) => (word.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean)
      .join(' ');
  };
  const hardenLinks = (root = document) => {
    if (root.matches && root.matches('a[href]')) {
      root.setAttribute('tabindex', '-1');
      root.setAttribute('draggable', 'false');
    }
    if (root.querySelectorAll) {
      root.querySelectorAll('a[href]').forEach((link) => {
        link.setAttribute('tabindex', '-1');
        link.setAttribute('draggable', 'false');
      });
    }
  };
  const restorePointerScroll = () => {
    const x = pointerScrollX;
    const y = pointerScrollY;
    requestAnimationFrame(() => window.scrollTo(x, y));
    setTimeout(() => window.scrollTo(x, y), 0);
  };
  const restoreRecentPointerScroll = () => {
    if (performance.now() - linkPointerAt < 1200) restorePointerScroll();
  };

  hardenLinks();
  new MutationObserver((records) => {
    records.forEach((record) =>
      record.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) hardenLinks(node);
      })
    );
  }).observe(document.documentElement, {childList: true, subtree: true});

  document.addEventListener('pointerdown', (event) => {
    if (!linkFor(event.target)) return;
    pointerScrollX = window.scrollX;
    pointerScrollY = window.scrollY;
    linkPointerAt = performance.now();
  }, true);

  document.addEventListener('focusin', (event) => {
    const link = linkFor(event.target);
    if (!link) return;
    link.blur();
    restoreRecentPointerScroll();
  }, true);

  document.addEventListener('click', (event) => {
    const link = linkFor(event.target);
    if (!link) return;
    link.blur();
    if (!event.ctrlKey) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
    restoreRecentPointerScroll();
  }, true);

  document.addEventListener('auxclick', (event) => {
    if (!linkFor(event.target) || event.ctrlKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    restoreRecentPointerScroll();
  }, true);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Control') document.documentElement.classList.add('lumen-ctrl-links');
  }, true);
  document.addEventListener('keyup', (event) => {
    if (event.key === 'Control') document.documentElement.classList.remove('lumen-ctrl-links');
  }, true);
  window.addEventListener('blur', () =>
    document.documentElement.classList.remove('lumen-ctrl-links'));
  return true;
})()
"""


SELECTION_CONTEXT_SCRIPT = r"""
(() => {
  const selection = window.getSelection ? window.getSelection() : null;
  const selectedText = selection
    ? (window.__lumenSelectedText ? window.__lumenSelectedText() : selection.toString())
    : '';
  if (!selection || !selection.rangeCount) {
    return JSON.stringify({selection: selectedText, context: ''});
  }
  const range = selection.getRangeAt(0);
  let node = range.commonAncestorContainer;
  let element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  if (!element) return JSON.stringify({selection: selectedText, context: ''});
  const endpoint = selection.anchorNode
    ? (selection.anchorNode.nodeType === Node.ELEMENT_NODE
        ? selection.anchorNode
        : selection.anchorNode.parentElement)
    : null;
  const pdfLine =
    (endpoint && endpoint.closest ? endpoint.closest('.pdf-text-line') : null)
    || element.closest('.pdf-text-line');
  if (pdfLine) {
    return JSON.stringify({
      selection: selectedText,
      context: (pdfLine.dataset.context || '').replace(/\s+/g, ' ').trim().slice(0, 1800)
    });
  }
  const selector = 'p,li,blockquote,h1,h2,h3,h4,h5,h6,figcaption,dt,dd,pre';
  const base = element.closest(selector) || element;
  const candidates = [
    base.previousElementSibling,
    base,
    base.nextElementSibling,
    base.nextElementSibling ? base.nextElementSibling.nextElementSibling : null
  ];
  const pieces = [];
  candidates.forEach((candidate) => {
    if (!candidate || pieces.join(' ').length >= 1600) return;
    const text = (candidate.innerText || candidate.textContent || '').replace(/\s+/g, ' ').trim();
    if (text && !pieces.includes(text)) pieces.push(text);
  });
  return JSON.stringify({selection: selectedText, context: pieces.join(' ').slice(0, 1800)});
})()
"""


# Installed only while the reader is choosing an RSVP starting word.  The
# overlays are separate from the book DOM, so targeting never edits the EPUB or
# changes the text sequence used by SpeedReadingDocument.
RSVP_TARGETING_SCRIPT = r"""
(() => {
  if (window.__lumenRsvpTargetStop) window.__lumenRsvpTargetStop();

  const ROOT_CLASS = 'lumen-rsvp-targeting';
  const TOKEN_RE = /\S+/gu;
  let candidate = null;
  let picked = null;

  const hud = document.createElement('div');
  hud.id = 'lumen-rsvp-target-hud';
  hud.innerHTML = `
    <span class="lumen-rsvp-target-icon">⌖</span>
    <span><b>POINT TO THE FIRST WORD</b><small>Move precisely, then click to launch RSVP · Esc cancels</small></span>`;
  const focus = document.createElement('div');
  focus.id = 'lumen-rsvp-target-focus';
  const tag = document.createElement('div');
  tag.id = 'lumen-rsvp-target-tag';
  tag.textContent = 'START HERE';
  document.documentElement.append(hud, focus, tag);

  const style = document.createElement('style');
  style.id = 'lumen-rsvp-target-style';
  style.textContent = `
    html.${ROOT_CLASS}, html.${ROOT_CLASS} body { cursor: crosshair !important; }
    html.${ROOT_CLASS} body * { cursor: crosshair !important; }
    #lumen-rsvp-target-hud {
      position: fixed; z-index: 2147483647; top: 18px; left: 50%; transform: translateX(-50%);
      display: flex; align-items: center; gap: 12px; min-width: 390px; padding: 11px 18px 12px;
      color: #f4fff9; background: rgba(7, 14, 18, .94); border: 1px solid rgba(118,255,178,.72);
      border-radius: 16px; box-shadow: 0 18px 55px rgba(0,0,0,.38), 0 0 30px rgba(118,255,178,.12);
      backdrop-filter: blur(14px); pointer-events: none; font: 700 12px/1.2 'Segoe UI', sans-serif;
      letter-spacing: .09em;
    }
    #lumen-rsvp-target-hud small { display: block; margin-top: 4px; color: #a9b8b3; font-size: 11px;
      font-weight: 500; letter-spacing: .01em; }
    .lumen-rsvp-target-icon { display: grid; place-items: center; width: 35px; height: 35px;
      color: #76ffb2; border: 1px solid rgba(118,255,178,.55); border-radius: 50%; font-size: 22px; }
    #lumen-rsvp-target-focus { position: fixed; z-index: 2147483645; display: none; pointer-events: none;
      border: 2px solid #76ffb2; border-radius: 5px; background: rgba(118,255,178,.20);
      box-shadow: 0 0 0 3px rgba(7,14,18,.68), 0 0 22px rgba(118,255,178,.72);
      transition: left 45ms linear, top 45ms linear, width 45ms linear, height 45ms linear; }
    #lumen-rsvp-target-focus::before, #lumen-rsvp-target-focus::after { content: ''; position: absolute;
      inset: -7px; border: 1px solid rgba(118,255,178,.42); border-left-color: transparent;
      border-right-color: transparent; border-radius: 8px; }
    #lumen-rsvp-target-focus::after { inset: -11px; opacity: .45; }
    #lumen-rsvp-target-tag { position: fixed; z-index: 2147483646; display: none; pointer-events: none;
      padding: 4px 8px; color: #07110d; background: #76ffb2; border-radius: 5px;
      box-shadow: 0 7px 18px rgba(0,0,0,.3); font: 800 9px/1 'Segoe UI', sans-serif;
      letter-spacing: .08em; white-space: nowrap; }
  `;
  document.head.appendChild(style);
  document.documentElement.classList.add(ROOT_CLASS);

  const allowedTextNode = (node) => {
    const parent = node && node.parentElement;
    return !!parent
      && !parent.closest('script,style,noscript,.lumen-section-label,#lumen-rsvp-target-hud,#lumen-rsvp-target-tag')
      && /\S/u.test(node.data || '');
  };

  const textNodes = () => {
    const nodes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => allowedTextNode(node)
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
    });
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  };

  const visibleWords = () => {
    const pdfWords = [...document.querySelectorAll('.pdf-word')];
    if (pdfWords.length) return pdfWords.map((word) => (word.textContent || '').trim());
    return textNodes().flatMap((node) => [...node.data.matchAll(TOKEN_RE)].map((match) => match[0]));
  };

  const rectForRange = (range) => {
    const rects = [...range.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0);
    if (!rects.length) return null;
    return rects.reduce((box, rect) => ({
      left: Math.min(box.left, rect.left), top: Math.min(box.top, rect.top),
      right: Math.max(box.right, rect.right), bottom: Math.max(box.bottom, rect.bottom)
    }), {left: rects[0].left, top: rects[0].top, right: rects[0].right, bottom: rects[0].bottom});
  };

  const wordAt = (x, y) => {
    const element = document.elementFromPoint(x, y);
    const pdfWord = element && element.closest ? element.closest('.pdf-word') : null;
    if (pdfWord) {
      const words = [...document.querySelectorAll('.pdf-word')];
      const index = words.indexOf(pdfWord);
      const rect = pdfWord.getBoundingClientRect();
      if (index < 0 || x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) return null;
      return {word: (pdfWord.textContent || '').trim(), wordIndex: index,
        rect: {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom}};
    }

    const caret = document.caretRangeFromPoint
      ? document.caretRangeFromPoint(x, y)
      : (() => {
          const position = document.caretPositionFromPoint ? document.caretPositionFromPoint(x, y) : null;
          if (!position) return null;
          const range = document.createRange();
          range.setStart(position.offsetNode, position.offset);
          range.collapse(true);
          return range;
        })();
    if (!caret || caret.startContainer.nodeType !== Node.TEXT_NODE) return null;
    const node = caret.startContainer;
    if (!allowedTextNode(node)) return null;
    const nodes = textNodes();
    const nodeIndex = nodes.indexOf(node);
    if (nodeIndex < 0) return null;
    let preceding = 0;
    for (let i = 0; i < nodeIndex; i += 1) preceding += [...nodes[i].data.matchAll(TOKEN_RE)].length;
    const matches = [...node.data.matchAll(TOKEN_RE)];
    for (let i = 0; i < matches.length; i += 1) {
      const match = matches[i];
      const range = document.createRange();
      range.setStart(node, match.index);
      range.setEnd(node, match.index + match[0].length);
      const rect = rectForRange(range);
      if (!rect) continue;
      if (x >= rect.left - 1 && x <= rect.right + 1 && y >= rect.top - 1 && y <= rect.bottom + 1) {
        return {word: match[0], wordIndex: preceding + i, rect};
      }
    }
    return null;
  };

  const paint = (next) => {
    candidate = next;
    if (!next) {
      focus.style.display = 'none';
      tag.style.display = 'none';
      return;
    }
    const rect = next.rect;
    focus.style.display = 'block';
    focus.style.left = `${rect.left - 4}px`;
    focus.style.top = `${rect.top - 3}px`;
    focus.style.width = `${Math.max(7, rect.right - rect.left + 8)}px`;
    focus.style.height = `${Math.max(7, rect.bottom - rect.top + 6)}px`;
    tag.style.display = 'block';
    tag.style.left = `${Math.max(8, Math.min(innerWidth - 82, rect.left - 3))}px`;
    tag.style.top = `${Math.max(8, rect.top - 25)}px`;
  };

  const onMove = (event) => paint(wordAt(event.clientX, event.clientY));
  const onDown = (event) => {
    if (event.button !== 0) return;
    const selected = wordAt(event.clientX, event.clientY);
    if (selected) {
      const words = visibleWords();
      picked = {
        word: selected.word,
        wordIndex: selected.wordIndex,
        contextBefore: words.slice(Math.max(0, selected.wordIndex - 6), selected.wordIndex),
        contextAfter: words.slice(selected.wordIndex + 1, selected.wordIndex + 7)
      };
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  const suppress = (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  const onLeave = () => paint(null);
  document.addEventListener('pointermove', onMove, true);
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('click', suppress, true);
  document.addEventListener('auxclick', suppress, true);
  document.addEventListener('mouseleave', onLeave, true);

  window.__lumenRsvpTargetTakePick = () => {
    const result = picked;
    picked = null;
    return result;
  };
  window.__lumenRsvpTargetStop = () => {
    document.removeEventListener('pointermove', onMove, true);
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('click', suppress, true);
    document.removeEventListener('auxclick', suppress, true);
    document.removeEventListener('mouseleave', onLeave, true);
    document.documentElement.classList.remove(ROOT_CLASS);
    hud.remove(); focus.remove(); tag.remove(); style.remove();
    delete window.__lumenRsvpTargetTakePick;
    delete window.__lumenRsvpTargetStop;
  };
  return true;
})()
"""

RSVP_TARGET_TAKE_PICK_SCRIPT = r"""
(() => JSON.stringify(window.__lumenRsvpTargetTakePick
  ? window.__lumenRsvpTargetTakePick()
  : null))()
"""

RSVP_TARGET_STOP_SCRIPT = r"""
(() => { if (window.__lumenRsvpTargetStop) window.__lumenRsvpTargetStop(); return true; })()
"""


RSVP_RETURN_HIGHLIGHT_SCRIPT = r"""
((startIndex, requestedCount) => {
  if (window.__lumenRsvpReturnHighlightStop) window.__lumenRsvpReturnHighlightStop();

  const TOKEN_RE = /\S+/gu;
  const count = Math.max(1, Number(requestedCount) || 1);
  const root = document.createElement('div');
  root.id = 'lumen-rsvp-return-highlight';
  const tag = document.createElement('div');
  tag.id = 'lumen-rsvp-return-tag';
  tag.textContent = count > 1 ? 'LAST PHRASE READ' : 'LAST WORD READ';
  const style = document.createElement('style');
  style.id = 'lumen-rsvp-return-style';
  style.textContent = `
    #lumen-rsvp-return-highlight { position: fixed; inset: 0; z-index: 2147483644;
      pointer-events: none; }
    .lumen-rsvp-return-segment { position: fixed; border: 2px solid #ff4d64; border-radius: 5px;
      background: rgba(255, 55, 82, .24); box-shadow: 0 0 0 3px rgba(22,5,9,.60),
      0 0 25px rgba(255,55,82,.66); }
    .lumen-rsvp-return-segment::after { content: ''; position: absolute; inset: -7px;
      border: 1px solid rgba(255,77,100,.42); border-left-color: transparent;
      border-right-color: transparent; border-radius: 8px; }
    #lumen-rsvp-return-tag { position: fixed; z-index: 2147483645; pointer-events: none;
      padding: 5px 9px; color: #fff5f6; background: #a9152d; border: 1px solid #ff7185;
      border-radius: 6px; box-shadow: 0 8px 22px rgba(0,0,0,.38);
      font: 800 9px/1 'Segoe UI', sans-serif; letter-spacing: .09em; white-space: nowrap; }
  `;
  document.head.appendChild(style);
  document.documentElement.append(root, tag);

  const allowedTextNode = (node) => {
    const parent = node && node.parentElement;
    return !!parent
      && !parent.closest('script,style,noscript,.lumen-section-label,#lumen-rsvp-return-highlight,#lumen-rsvp-return-tag')
      && /\S/u.test(node.data || '');
  };
  const ranges = [];
  const pdfWords = [...document.querySelectorAll('.pdf-word')];
  if (pdfWords.length) {
    pdfWords.slice(startIndex, startIndex + count).forEach((word) => ranges.push(word));
  } else {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => allowedTextNode(node)
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
    });
    let index = 0;
    while (walker.nextNode() && ranges.length < count) {
      const node = walker.currentNode;
      for (const match of node.data.matchAll(TOKEN_RE)) {
        if (index >= startIndex && index < startIndex + count) {
          const range = document.createRange();
          range.setStart(node, match.index);
          range.setEnd(node, match.index + match[0].length);
          ranges.push(range);
        }
        index += 1;
        if (index >= startIndex + count) break;
      }
    }
  }

  const stop = () => {
    root.remove(); tag.remove(); style.remove();
    delete window.__lumenRsvpReturnHighlightStop;
  };
  window.__lumenRsvpReturnHighlightStop = stop;
  if (!ranges.length) {
    stop();
    return JSON.stringify({found: false});
  }

  const rawRects = () => ranges.flatMap((item) => {
    const rects = item instanceof Element
      ? [item.getBoundingClientRect()]
      : [...item.getClientRects()];
    return rects.filter((rect) => rect.width > 0 && rect.height > 0);
  });
  const initial = rawRects()[0];
  if (initial) window.scrollBy(0, initial.top - innerHeight * .48);

  const paint = () => {
    const rects = rawRects();
    if (!rects.length) return;
    const lines = [];
    rects.forEach((rect) => {
      const prior = lines[lines.length - 1];
      if (prior && Math.abs(prior.top - rect.top) < 4 && rect.left - prior.right < 14) {
        prior.right = Math.max(prior.right, rect.right);
        prior.bottom = Math.max(prior.bottom, rect.bottom);
      } else {
        lines.push({left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom});
      }
    });
    root.replaceChildren();
    lines.forEach((rect) => {
      const segment = document.createElement('div');
      segment.className = 'lumen-rsvp-return-segment';
      segment.style.left = `${rect.left - 4}px`;
      segment.style.top = `${rect.top - 3}px`;
      segment.style.width = `${Math.max(7, rect.right - rect.left + 8)}px`;
      segment.style.height = `${Math.max(7, rect.bottom - rect.top + 6)}px`;
      root.appendChild(segment);
    });
    const first = lines[0];
    tag.style.left = `${Math.max(8, Math.min(innerWidth - 118, first.left - 3))}px`;
    tag.style.top = `${Math.max(8, first.top - 28)}px`;
  };
  requestAnimationFrame(() => requestAnimationFrame(paint));
  return JSON.stringify({found: true, wordIndex: startIndex, wordCount: ranges.length});
})(__LUMEN_START_INDEX__, __LUMEN_WORD_COUNT__)
"""

RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT = r"""
(() => {
  if (window.__lumenRsvpReturnHighlightStop) window.__lumenRsvpReturnHighlightStop();
  return true;
})()
"""


def rsvp_return_highlight_script(word_index: int, word_count: int) -> str:
    """Build the trusted script for one exact, non-persistent RSVP return marker."""
    return RSVP_RETURN_HIGHLIGHT_SCRIPT.replace(
        "__LUMEN_START_INDEX__", str(max(0, int(word_index)))
    ).replace("__LUMEN_WORD_COUNT__", str(max(1, int(word_count))))


def resolve_rsvp_target_word_index(
    words: Sequence[str], payload: dict[str, Any]
) -> int | None:
    """Map a clicked DOM token back to the matching RSVP word conservatively."""
    try:
        dom_index = int(payload["wordIndex"])
    except (KeyError, TypeError, ValueError):
        return None

    clicked_word = str(payload.get("word") or "")
    if not clicked_word:
        return None
    if 0 <= dom_index < len(words) and words[dom_index] == clicked_word:
        return dom_index

    before_value = payload.get("contextBefore")
    after_value = payload.get("contextAfter")
    before = (
        [str(token) for token in before_value[-6:]]
        if isinstance(before_value, list)
        else []
    )
    after = (
        [str(token) for token in after_value[:6]]
        if isinstance(after_value, list)
        else []
    )
    candidates = [index for index, word in enumerate(words) if word == clicked_word]

    if not before and not after:
        nearby = [index for index in candidates if abs(index - dom_index) <= 8]
        return nearby[0] if len(nearby) == 1 else None

    def context_score(index: int) -> int:
        score = 0
        for offset, token in enumerate(reversed(before), 1):
            if index - offset < 0 or words[index - offset] != token:
                break
            score += 1
        for offset, token in enumerate(after, 1):
            if index + offset >= len(words) or words[index + offset] != token:
                break
            score += 1
        return score

    scored = [(context_score(index), index) for index in candidates]
    if not scored:
        return None
    best_score = max(score for score, _index in scored)
    required_score = min(2, len(before) + len(after))
    best = [index for score, index in scored if score == best_score]
    return best[0] if best_score >= required_score and len(best) == 1 else None


def control_link_activation_allowed(modifiers: Qt.KeyboardModifier) -> bool:
    """Return whether a physical Ctrl modifier authorizes a link action."""
    return bool(modifiers & Qt.KeyboardModifier.ControlModifier)


class SourcePathLabel(QLabel):
    """One-line original path that keeps the root and filename visible."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._source_path = ""
        self.setObjectName("sourcePathLabel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hide()

    @property
    def source_path(self) -> str:
        return getattr(self, "_source_path", "")

    def set_source_path(self, path: str | Path | None) -> None:
        self._source_path = "" if path is None else str(path)
        self.setAccessibleName(
            f"Original file: {self._source_path}" if self._source_path else "No book file open"
        )
        self.setToolTip(
            f"Original file\n{self._source_path}\nUse the copy button to copy this path."
            if self._source_path
            else ""
        )
        self._refresh_text()
        self.setVisible(bool(self._source_path))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 2)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._refresh_text()

    def _refresh_text(self) -> None:
        path = getattr(self, "_source_path", "")
        if not path:
            super().setText("")
            return
        super().setText(
            source_path_text(path, self.fontMetrics(), self.contentsRect().width())
        )


def _enable_precision_scrolling(view: QAbstractItemView) -> None:
    """Use pixel scrolling for trackpads and kinetic scrolling for touch input."""
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
    QScroller.grabGesture(view.viewport(), QScroller.ScrollerGestureType.TouchGesture)


class ReaderWebPage(QWebEnginePage):
    link_clicked = Signal(QUrl)

    def acceptNavigationRequest(
        self,
        url: QUrl,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            # Never allow Chromium to navigate the reading surface itself.
            # Only a deliberate Ctrl+click is forwarded to ReaderWindow.
            if control_link_activation_allowed(QApplication.keyboardModifiers()):
                self.link_clicked.emit(url)
            return False
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


class WelcomePage(QWidget):
    browse_requested = Signal()
    book_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._all_books: list[dict[str, str]] = []
        self.setObjectName("welcomePage")
        root = QVBoxLayout(self)
        root.setContentsMargins(70, 55, 70, 55)
        root.setSpacing(18)

        mark = QLabel("L")
        mark.setObjectName("heroMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(72, 72)
        root.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)

        heading = QLabel("Your books, beautifully focused.")
        heading.setObjectName("heroHeading")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(heading)

        subheading = QLabel(
            "Open an EPUB or PDF and settle into a clean, distraction-free reading space.\n"
            "Drop a book anywhere in this window, or choose one below."
        )
        subheading.setObjectName("heroSubheading")
        subheading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subheading)

        open_button = QPushButton("Open a Book")
        open_button.setObjectName("primaryButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setFixedSize(180, 46)
        open_button.clicked.connect(self.browse_requested)
        root.addWidget(open_button, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addSpacing(16)

        label = QLabel("ON THIS SHELF")
        label.setObjectName("eyebrow")
        label.setMaximumWidth(720)
        root.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.shelf_search = QLineEdit()
        self.shelf_search.setObjectName("shelfSearch")
        self.shelf_search.setPlaceholderText("Search titles, authors, or filenames…")
        self.shelf_search.setClearButtonEnabled(True)
        self.shelf_search.setMaximumWidth(720)
        self.shelf_search.setMinimumWidth(600)
        self.shelf_search.setAccessibleName("Search your bookshelf")
        self.shelf_search.textChanged.connect(self._filter_books)
        root.addWidget(self.shelf_search, 0, Qt.AlignmentFlag.AlignHCenter)

        self.books = QListWidget()
        self.books.setObjectName("shelfList")
        self.books.setMaximumWidth(720)
        self.books.setMinimumWidth(600)
        self.books.setMinimumHeight(170)
        self.books.setSpacing(5)
        self.books.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.books.itemDoubleClicked.connect(self._activate)
        _enable_precision_scrolling(self.books)
        root.addWidget(self.books, 1, Qt.AlignmentFlag.AlignHCenter)

    def set_books(self, books: list[dict[str, str]]) -> None:
        self._all_books = []
        seen: set[str] = set()
        for book in books:
            path = str(Path(book["path"]).resolve())
            if path in seen or not Path(path).is_file():
                continue
            seen.add(path)
            self._all_books.append(
                {
                    "path": path,
                    "title": str(book.get("title") or Path(path).stem),
                    "author": str(book.get("author") or generic_document_label(path)),
                }
            )
        self._filter_books(self.shelf_search.text())

    def _filter_books(self, query: str) -> None:
        self.books.clear()
        terms = query.casefold().split()
        matches = []
        for book in self._all_books:
            haystack = f"{book['title']} {book['author']} {Path(book['path']).name}".casefold()
            if all(term in haystack for term in terms):
                matches.append(book)
        for book in matches:
            path = book["path"]
            item = QListWidgetItem(
                f"{book.get('title') or Path(path).stem}\n"
                f"{book.get('author') or generic_document_label(path)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setSizeHint(QSize(0, 66))
            self.books.addItem(item)
        if not self.books.count():
            message = (
                "No matching books\nTry another title, author, or filename"
                if terms
                else "No EPUB or PDF books found yet\nOpen a book to begin your shelf"
            )
            empty = QListWidgetItem(message)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.books.addItem(empty)

    def _activate(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.book_requested.emit(path)


class DefinitionBlock(QFrame):
    """One immutable contribution to an aggregated definition card."""

    def __init__(self, entry: DictionaryEntry, selected_text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("definitionBlock")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(6)

        source_text = f"DEFINITION FROM  ·  {entry.source}"
        if entry.word.casefold() != selected_text.casefold():
            source_text += f"  ·  {entry.word}"
        source = QLabel(source_text)
        source.setObjectName("definitionBlockSource")
        source.setWordWrap(True)
        source.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(source)

        definition = QLabel("• " + entry.definition.replace("\n• ", "\n• "))
        definition.setObjectName("definitionText")
        definition.setWordWrap(True)
        definition.setTextFormat(Qt.TextFormat.PlainText)
        definition.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(definition)

        if entry.example and len(entry.example) <= 260:
            example = QLabel(f"“{entry.example}”")
            example.setObjectName("definitionExample")
            example.setWordWrap(True)
            example.setTextFormat(Qt.TextFormat.PlainText)
            example.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(example)
        if entry.synonyms:
            synonyms = QLabel("Synonyms  ·  " + ", ".join(entry.synonyms))
            synonyms.setObjectName("definitionSynonyms")
            synonyms.setWordWrap(True)
            synonyms.setTextFormat(Qt.TextFormat.PlainText)
            synonyms.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(synonyms)


class SelectionLookupPrompt(QFrame):
    """Clickable action bubble shown after a deliberate text drag selection."""

    lookup_requested = Signal(str, QPoint)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._selection = ""
        self._anchor = QPoint()
        self.setObjectName("selectionLookupPrompt")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 145))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        self.button = QPushButton()
        self.button.setObjectName("selectionLookupButton")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self._activate)
        layout.addWidget(self.button)

        self.animation = QPropertyAnimation(self, b"geometry", self)
        self.animation.setDuration(230)
        self.animation.setEasingCurve(QEasingCurve.Type.OutBack)
        self.hide()

    @property
    def selection(self) -> str:
        return self._selection

    @property
    def is_multiword(self) -> bool:
        return len(self._selection.split()) > 1

    def show_for(self, selection: str, global_position: QPoint) -> None:
        self._selection = selection
        self._anchor = QPoint(global_position)
        preview = selection if len(selection) <= 52 else selection[:49].rstrip() + "…"
        action = "DEFINE SELECTED PHRASE" if self.is_multiword else "DEFINE SELECTED WORD"
        self.button.setText(f"📖  ❔   {action}\n“{preview}”")
        self.button.setAccessibleName(f"Look up the selected text: {selection}")
        self.adjustSize()

        parent = self.parentWidget()
        if parent is None:
            return
        anchor = parent.mapFromGlobal(global_position)
        width = min(max(self.sizeHint().width(), 280), 430)
        height = self.sizeHint().height()
        x = anchor.x() + 14
        y = anchor.y() + 16
        if x + width > parent.width() - 12:
            x = anchor.x() - width - 14
        if y + height > parent.height() - 12:
            y = anchor.y() - height - 16
        x = max(12, min(x, parent.width() - width - 12))
        y = max(76, min(y, parent.height() - height - 12))
        final_geometry = QRect(x, y, width, height)
        start_geometry = QRect(x + 14, y + 6, max(40, width - 28), max(30, height - 12))
        self.animation.stop()
        self.animation.setStartValue(start_geometry)
        self.animation.setEndValue(final_geometry)
        self.setGeometry(start_geometry)
        self.show()
        self.raise_()
        self.animation.start()

    def dismiss(self) -> None:
        self.animation.stop()
        self.hide()

    def _activate(self) -> None:
        selection = self._selection
        anchor = QPoint(self._anchor)
        self.dismiss()
        if selection:
            self.lookup_requested.emit(selection, anchor)

class DefinitionCard(QFrame):
    """Animated, append-only card that aggregates several dictionary sources."""

    dismissed = Signal()
    retry_requested = Signal()
    _SPINNER_FRAMES = ("◴", "◷", "◶", "◵")

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._anchor = QPoint()
        self._selection = ""
        self._entries: list[DictionaryEntry] = []
        self._seen_definitions: set[str] = set()
        self._spinner_index = 0
        self.setObjectName("definitionCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(470)
        self.setMaximumHeight(610)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 11)
        shadow.setColor(QColor(0, 0, 0, 135))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 17)
        layout.setSpacing(8)
        top = QHBoxLayout()
        eyebrow = QLabel("QUICK DEFINITION")
        eyebrow.setObjectName("definitionEyebrow")
        top.addWidget(eyebrow, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("definitionClose")
        close_button.setFixedSize(28, 28)
        close_button.setAccessibleName("Close definition")
        close_button.clicked.connect(self.dismiss)
        top.addWidget(close_button)
        layout.addLayout(top)

        self.word_label = QLabel()
        self.word_label.setObjectName("definitionWord")
        self.word_label.setWordWrap(True)
        self.word_label.setTextFormat(Qt.TextFormat.PlainText)
        self.word_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.word_label)

        self.meta_label = QLabel()
        self.meta_label.setObjectName("definitionMeta")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.meta_label)

        status_row = QHBoxLayout()
        status_row.setSpacing(7)
        self.spinner_label = QLabel("◴")
        self.spinner_label.setObjectName("definitionSpinner")
        self.spinner_label.setFixedWidth(18)
        status_row.addWidget(self.spinner_label)
        self.status_label = QLabel("Gathering definitions…")
        self.status_label.setObjectName("definitionStatus")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)

        self.progress = QProgressBar()
        self.progress.setObjectName("definitionProgress")
        self.progress.setRange(0, 20000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        layout.addWidget(self.progress)

        self.definitions_scroll = QScrollArea()
        self.definitions_scroll.setObjectName("definitionScroll")
        self.definitions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.definitions_scroll.setWidgetResizable(True)
        self.definitions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.definitions_scroll.setMinimumHeight(88)
        self.definitions_scroll.setMaximumHeight(360)
        self.definitions_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(
            self.definitions_scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture
        )
        self.definitions_content = QWidget()
        self.definitions_content.setObjectName("definitionContent")
        self.definitions_layout = QVBoxLayout(self.definitions_content)
        self.definitions_layout.setContentsMargins(0, 0, 3, 0)
        self.definitions_layout.setSpacing(8)
        self.definitions_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty_label = QLabel("Searching local and online sources for a complete answer…")
        self.empty_label.setObjectName("definitionEmpty")
        self.empty_label.setWordWrap(True)
        self.definitions_layout.addWidget(self.empty_label)
        self.definitions_scroll.setWidget(self.definitions_content)
        layout.addWidget(self.definitions_scroll)

        self.retry_button = QPushButton("↻  Start a new 20-second lookup")
        self.retry_button.setObjectName("definitionRetry")
        self.retry_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_button.clicked.connect(self.retry_requested)
        self.retry_button.hide()
        layout.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.source_label = QLabel("Incoming definitions are appended; existing ones never change.")
        self.source_label.setObjectName("definitionSource")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(140)
        self.animation_timer.timeout.connect(self._animate_spinner)
        self.hide()

    @property
    def definition_count(self) -> int:
        return len(self._entries)

    @property
    def source_count(self) -> int:
        return len({entry.source for entry in self._entries})

    def dismiss(self) -> None:
        self.animation_timer.stop()
        self.hide()
        self.dismissed.emit()

    def begin_lookup(self, text: str, global_position: QPoint) -> None:
        self._anchor = QPoint(global_position)
        self._selection = text
        self._entries.clear()
        self._seen_definitions.clear()
        for block in self.definitions_content.findChildren(DefinitionBlock):
            self.definitions_layout.removeWidget(block)
            block.deleteLater()
        self.word_label.setText(text)
        self.meta_label.setText("Understanding the complete phrase…" if " " in text else "Gathering word senses…")
        self.empty_label.show()
        self.empty_label.setText("Searching local and online sources for a complete answer…")
        self.definitions_scroll.setFixedHeight(88)
        self.retry_button.hide()
        self.spinner_label.show()
        self.progress.show()
        self.progress.setValue(0)
        self.status_label.setText("Starting a 20-second multi-source lookup…")
        self.animation_timer.start()
        self._present()

    def add_entry(self, entry: DictionaryEntry) -> bool:
        key = " ".join(entry.definition.casefold().split())
        if not key or key in self._seen_definitions:
            return False
        self._seen_definitions.add(key)
        self._entries.append(entry)
        self.empty_label.hide()
        self.definitions_layout.addWidget(DefinitionBlock(entry, self._selection, self.definitions_content))
        self._update_meta()
        self._resize_content()
        self._present()
        return True

    def set_progress(self, seconds_remaining: float, pending_sources: int) -> None:
        seconds_remaining = max(0.0, min(seconds_remaining, 20.0))
        self.progress.setValue(round((20.0 - seconds_remaining) * 1000))
        count = self.definition_count
        suffix = "definition" if count == 1 else "definitions"
        if count:
            self.status_label.setText(
                f"{count} {suffix} ready · seeking {pending_sources} more source"
                f"{'s' if pending_sources != 1 else ''} · {max(0, int(seconds_remaining + 0.99))}s"
            )
        elif pending_sources:
            self.status_label.setText(
                f"Consulting {pending_sources} source{'s' if pending_sources != 1 else ''} · "
                f"{max(0, int(seconds_remaining + 0.99))}s"
            )
        else:
            self.status_label.setText(
                f"No exact result yet · exploring alternate interpretations · "
                f"{max(0, int(seconds_remaining + 0.99))}s"
            )

    def finish(self, message: str) -> None:
        self.animation_timer.stop()
        self.spinner_label.setText("✓")
        self.progress.setValue(self.progress.maximum())
        self.status_label.setText(message)
        self._present()

    def show_error(self, word: str, message: str, *, retryable: bool = False) -> None:
        self.animation_timer.stop()
        self.word_label.setText(word)
        self.meta_label.setText("No definition arrived")
        self.empty_label.setText(message)
        self.empty_label.show()
        self.retry_button.setVisible(retryable)
        self.spinner_label.setText("!")
        self.progress.setValue(self.progress.maximum())
        self.status_label.setText(message)
        self._present()

    def _update_meta(self) -> None:
        phonetics = list(dict.fromkeys(entry.phonetic for entry in self._entries if entry.phonetic))
        parts = list(dict.fromkeys(entry.part_of_speech for entry in self._entries if entry.part_of_speech))
        values = phonetics[:1] + parts[:3]
        self.meta_label.setText("  ·  ".join(values) or ("phrase" if " " in self._selection else "word"))

    def _animate_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(self._SPINNER_FRAMES)
        self.spinner_label.setText(self._SPINNER_FRAMES[self._spinner_index])

    def _resize_content(self) -> None:
        self.definitions_content.adjustSize()
        target = max(88, min(360, self.definitions_content.sizeHint().height() + 6))
        self.definitions_scroll.setFixedHeight(target)

    def _present(self) -> None:
        self.show()
        self.adjustSize()
        self._place()
        self.raise_()

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        anchor = parent.mapFromGlobal(self._anchor)
        width = self.width()
        height = min(self.sizeHint().height(), self.maximumHeight(), max(parent.height() - 92, 260))
        self.resize(width, height)
        x = anchor.x() + 18
        y = anchor.y() + 18
        if x + width > parent.width() - 12:
            x = anchor.x() - width - 18
        if y + height > parent.height() - 12:
            y = anchor.y() - height - 18
        x = max(12, min(x, parent.width() - width - 12))
        y = max(76, min(y, parent.height() - height - 12))
        self.move(x, y)


class MarkPositionDialog(ScreenFittingDialog):
    """Collect an optional note and tags for a reading position."""

    def __init__(
        self,
        parent: QWidget,
        *,
        book_title: str,
        chapter_title: str,
        progress_text: str,
        quote: str = "",
        note: str = "",
        tags: list[str] | None = None,
        editing: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("markEditor")
        self.setWindowTitle("Edit reading note" if editing else "Mark this reading position")
        self.setModal(True)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Edit note" if editing else "Mark position")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        context = QLabel(f"{book_title}\n{chapter_title}  ·  {progress_text}")
        context.setObjectName("markContext")
        context.setWordWrap(True)
        context.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(context)

        if quote:
            quote_label = QLabel(f"Selected text\n“{quote[:500]}”")
            quote_label.setObjectName("markQuote")
            quote_label.setWordWrap(True)
            quote_label.setTextFormat(Qt.TextFormat.PlainText)
            quote_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(quote_label)

        form = QFormLayout()
        form.setContentsMargins(0, 2, 0, 0)
        form.setSpacing(8)
        self.note_edit = QTextEdit()
        self.note_edit.setObjectName("markNoteEdit")
        self.note_edit.setPlaceholderText(
            "Add a thought, question, summary, or reminder… (optional)"
        )
        self.note_edit.setPlainText(note)
        self.note_edit.setFixedHeight(125)
        form.addRow("Comment", self.note_edit)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("research, favorite, review")
        self.tags_edit.setText(", ".join(tags or []))
        form.addRow("Tags", self.tags_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("Save note" if editing else "Save mark")
        save_button.setObjectName("primarySmallButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.note_edit.setFocus()

    @property
    def note(self) -> str:
        return self.note_edit.toPlainText().strip()

    @property
    def tags(self) -> list[str]:
        return [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]


class MarksManagerDialog(ScreenFittingDialog):
    """Search, edit, and open notes from every book in the library."""

    open_requested = Signal(str)
    changed = Signal()

    def __init__(self, parent: QWidget, store: MarksStore):
        super().__init__(parent)
        self.store = store
        self.setObjectName("marksManager")
        self.setWindowTitle("Notes & Reading Marks — Lumen")
        self.setMinimumSize(760, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("Notes & Reading Marks")
        title.setObjectName("dialogHeading")
        title_row.addWidget(title, 1)
        folder_button = QPushButton("Open data folder")
        folder_button.setObjectName("subtleButton")
        folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.path.parent)))
        )
        title_row.addWidget(folder_button)
        layout.addLayout(title_row)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("marksSummary")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search notes, quotes, tags, books, authors, or chapters…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.refresh)
        layout.addWidget(self.search_edit)

        self.list = QListWidget()
        self.list.setObjectName("marksList")
        self.list.setWordWrap(True)
        self.list.setSpacing(5)
        self.list.itemActivated.connect(lambda _item: self.open_selected())
        self.list.itemSelectionChanged.connect(self._update_actions)
        _enable_precision_scrolling(self.list)
        layout.addWidget(self.list, 1)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open location")
        self.open_button.setObjectName("primarySmallButton")
        self.open_button.clicked.connect(self.open_selected)
        actions.addWidget(self.open_button)
        self.edit_button = QPushButton("Edit note")
        self.edit_button.setObjectName("subtleButton")
        self.edit_button.clicked.connect(self.edit_selected)
        actions.addWidget(self.edit_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("subtleButton")
        self.delete_button.clicked.connect(self.delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        close_button = QPushButton("Close")
        close_button.setObjectName("subtleButton")
        close_button.clicked.connect(self.close)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.refresh()

    def showEvent(self, event: Any) -> None:
        self.store.load()
        self.refresh()
        super().showEvent(event)

    def selected_mark(self) -> ReadingMark | None:
        item = self.list.currentItem()
        return self.store.get(str(item.data(Qt.ItemDataRole.UserRole))) if item else None

    def refresh(self, _query: str = "") -> None:
        selected = self.selected_mark()
        selected_id = selected.id if selected else ""
        marks = self.store.search(self.search_edit.text())
        self.list.clear()
        for mark in marks:
            percent = round(mark.overall_percent * 100)
            tags = "  ".join(f"#{tag}" for tag in mark.tags)
            detail = f"{mark.chapter_title}  ·  {percent}% through book"
            summary = mark.summary.replace("\n", " ")
            if len(summary) > 150:
                summary = summary[:147] + "…"
            text = f"{mark.book_title}\n{detail}\n{summary}"
            if tags:
                text += f"\n{tags}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, mark.id)
            item.setToolTip(mark.book_path)
            item.setSizeHint(QSize(0, 96 if tags else 80))
            self.list.addItem(item)
            if mark.id == selected_id:
                self.list.setCurrentItem(item)
        visible = len(marks)
        total = len(self.store.marks)
        self.summary_label.setText(
            f"{visible} of {total} marks shown  ·  Stored in {self.store.path.name}"
        )
        if not marks:
            empty = QListWidgetItem(
                "No matching notes or marks" if self.search_edit.text() else "No reading marks yet"
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        self._update_actions()

    def _update_actions(self) -> None:
        enabled = self.selected_mark() is not None
        self.open_button.setEnabled(enabled)
        self.edit_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def open_selected(self) -> None:
        mark = self.selected_mark()
        if mark is None:
            return
        self.close()
        self.open_requested.emit(mark.id)

    def edit_selected(self) -> None:
        mark = self.selected_mark()
        if mark is None:
            return
        dialog = MarkPositionDialog(
            self,
            book_title=mark.book_title,
            chapter_title=mark.chapter_title,
            progress_text=f"{round(mark.overall_percent * 100)}% through book",
            quote=mark.quote,
            note=mark.note,
            tags=mark.tags,
            editing=True,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.update(mark.id, note=dialog.note, tags=dialog.tags)
            self.refresh()
            self.changed.emit()

    def delete_selected(self) -> None:
        mark = self.selected_mark()
        if mark is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete reading mark?",
            f"Delete the mark in “{mark.book_title}” — {mark.chapter_title}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.store.remove(mark.id)
            self.refresh()
            self.changed.emit()


class DefinitionSettingsDialog(ScreenFittingDialog):
    """Configuration and live Ollama discovery for the deep-definition ladder."""

    def __init__(self, parent: QWidget, values: dict[str, Any]):
        super().__init__(parent)
        defaults = default_definition_fallbacks()
        self._values = {**defaults, **(values if isinstance(values, dict) else {})}
        self._model_reply: QNetworkReply | None = None
        self.setObjectName("definitionSettings")
        self.setWindowTitle("Deep Definition — Expert Sources")
        self.setModal(True)
        self.setMinimumWidth(650)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(13)

        heading = QLabel("Deep Definition")
        heading.setObjectName("dialogHeading")
        root.addWidget(heading)
        intro = QLabel(
            "When ordinary dictionaries miss a coined, inflected, technical, or contextual "
            "expression, Lumen can combine transparent local analysis, Tlamatini web evidence, "
            "and an optional Ollama lexicographer. Every source is appended and clearly labeled."
        )
        intro.setObjectName("dialogIntro")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.contextual_check = QCheckBox("Contextual morphology and compound analysis")
        self.contextual_check.setChecked(bool(self._values["contextual_inference"]))
        self.contextual_check.setToolTip(
            "Fast, offline, and transparent. Inferred definitions are always labeled as inferred."
        )
        root.addWidget(self.contextual_check)

        googler_title = QLabel("TLAMATINI GOOGLER  ·  EXACT WEB EVIDENCE")
        googler_title.setObjectName("eyebrow")
        root.addWidget(googler_title)
        self.googler_check = QCheckBox("Enable the Tlamatini Googler fallback")
        self.googler_check.setChecked(bool(self._values["googler_enabled"]))
        root.addWidget(self.googler_check)
        googler_row = QHBoxLayout()
        self.googler_path = QLineEdit(str(self._values["googler_path"]))
        self.googler_path.setPlaceholderText(str(DEFAULT_GOOGLER_PATH))
        self.googler_path.textChanged.connect(self._update_googler_status)
        googler_row.addWidget(self.googler_path, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("subtleButton")
        browse.clicked.connect(self._browse_googler)
        googler_row.addWidget(browse)
        root.addLayout(googler_row)
        self.googler_status = QLabel()
        self.googler_status.setObjectName("settingsStatus")
        root.addWidget(self.googler_status)

        ollama_title = QLabel("OLLAMA  ·  CONTEXTUAL AI DEFINER")
        ollama_title.setObjectName("eyebrow")
        root.addWidget(ollama_title)
        self.ollama_check = QCheckBox("Enable Ollama only after conventional sources miss")
        self.ollama_check.setChecked(bool(self._values["ollama_enabled"]))
        root.addWidget(self.ollama_check)

        form = QFormLayout()
        form.setSpacing(9)
        self.ollama_url = QLineEdit(str(self._values["ollama_url"]))
        self.ollama_url.setPlaceholderText(DEFAULT_OLLAMA_URL)
        form.addRow("Ollama host", self.ollama_url)
        model_row = QHBoxLayout()
        self.ollama_model = WheelSafeComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.addItem(str(self._values["ollama_model"] or DEFAULT_OLLAMA_MODEL))
        if self.ollama_model.itemText(0) != DEFAULT_OLLAMA_MODEL:
            self.ollama_model.addItem(DEFAULT_OLLAMA_MODEL)
        model_row.addWidget(self.ollama_model, 1)
        refresh = QPushButton("Discover models")
        refresh.setObjectName("subtleButton")
        refresh.clicked.connect(self._refresh_models)
        model_row.addWidget(refresh)
        form.addRow("Model", model_row)
        root.addLayout(form)
        local_hint = QLabel(
            "Use the local Ollama host for both local and signed-in cloud models. "
            "Example cloud tag: glm-5.2:cloud. Lumen never stores an Ollama password or API key."
        )
        local_hint.setObjectName("dialogIntro")
        local_hint.setWordWrap(True)
        root.addWidget(local_hint)
        self.ollama_status = QLabel("Select “Discover models” to test the configured host.")
        self.ollama_status.setObjectName("settingsStatus")
        self.ollama_status.setWordWrap(True)
        root.addWidget(self.ollama_status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save definition sources")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._network = QNetworkAccessManager(self)
        self._update_googler_status()
        QTimer.singleShot(120, self._refresh_models)

    def values(self) -> dict[str, Any]:
        return {
            "contextual_inference": self.contextual_check.isChecked(),
            "googler_enabled": self.googler_check.isChecked(),
            "googler_path": self.googler_path.text().strip() or str(DEFAULT_GOOGLER_PATH),
            "ollama_enabled": self.ollama_check.isChecked(),
            "ollama_url": self.ollama_url.text().strip() or DEFAULT_OLLAMA_URL,
            "ollama_model": self.ollama_model.currentText().strip() or DEFAULT_OLLAMA_MODEL,
        }

    def _browse_googler(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose the Tlamatini Googler directory",
            self.googler_path.text().strip() or str(DEFAULT_GOOGLER_PATH),
        )
        if selected:
            self.googler_path.setText(selected)

    def _update_googler_status(self) -> None:
        script = Path(self.googler_path.text().strip()) / "googler.py"
        if script.is_file():
            self.googler_status.setText("✓ Googler agent found. It will run in an isolated worker.")
        else:
            self.googler_status.setText("! googler.py was not found at this location.")

    def _refresh_models(self) -> None:
        if self._model_reply is not None and self._model_reply.isRunning():
            self._model_reply.abort()
        url = normalized_ollama_url(self.ollama_url.text(), "tags")
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Lumen Book Reader/1.1")
        request.setTransferTimeout(4500)
        self.ollama_status.setText("◌ Contacting Ollama and discovering available models…")
        reply = self._network.get(request)
        self._model_reply = reply
        reply.finished.connect(lambda current=reply: self._models_received(current))

    def _models_received(self, reply: QNetworkReply) -> None:
        if reply is not self._model_reply:
            reply.deleteLater()
            return
        self._model_reply = None
        error = reply.error()
        payload = bytes(reply.readAll()) if error == QNetworkReply.NetworkError.NoError else b""
        detail = reply.errorString()
        reply.deleteLater()
        if error != QNetworkReply.NetworkError.NoError:
            self.ollama_status.setText(
                "Ollama is offline at this address. Start Ollama, then select Discover models. "
                f"Technical detail: {detail}"
            )
            return
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        models = [
            str(item.get("name") or item.get("model") or "").strip()
            for item in data.get("models", [])
            if isinstance(item, dict)
        ]
        models = [model for model in models if model]
        current = self.ollama_model.currentText().strip()
        self.ollama_model.clear()
        self.ollama_model.addItems(models)
        if current and current not in models:
            self.ollama_model.addItem(current)
        if DEFAULT_OLLAMA_MODEL not in models and DEFAULT_OLLAMA_MODEL != current:
            self.ollama_model.addItem(DEFAULT_OLLAMA_MODEL)
        self.ollama_model.setCurrentText(current or (models[0] if models else DEFAULT_OLLAMA_MODEL))
        if models:
            self.ollama_status.setText(
                f"✓ Connected. {len(models)} available model{'s' if len(models) != 1 else ''} discovered."
            )
        else:
            self.ollama_status.setText(
                "Connected, but no models are installed. Pull a local or cloud model with Ollama first."
            )


class ReaderWindow(QMainWindow):
    offline_dictionary_ready = Signal(str, object, int)
    deep_dictionary_ready = Signal(str, object, str, int)

    def __init__(
        self,
        store: ReaderStore,
        initial_books: list[Path] | None = None,
        marks_store: MarksStore | None = None,
        library_root: str | Path | None = None,
        library_index: LibraryIndex | None = None,
    ):
        super().__init__()
        self.store = store
        self.library_root = normalize_root(library_root or Path.cwd())
        self.library_index = library_index or LibraryIndex(default_index_path())
        self._scanner: TurboScanner | None = None
        self._monitor: ScanMonitorDialog | None = None
        self._sweep_timer: QTimer | None = None
        self.theme_colors: dict[str, str] = {}
        self.marks_store = marks_store or MarksStore(Path.cwd() / MARKS_FILENAME)
        self.book: EpubBook | PdfBook | None = None
        self.chapter_index = 0
        self.scroll_percent = 0.0
        self.pending_scroll = 0.0
        self.pending_find = ""
        self.pending_find_backward = False
        self._reader_search_scope = "book"
        self._reader_search_backward = False
        self._reader_search_results: list[SearchResult] = []
        self._reader_search_position = -1
        self._reader_search_occurrence = 0
        self._reader_search_session: tuple[str, bool, int] | None = None
        self._slider_is_dragging = False
        self._building_toc = False
        self.dictionary_cache = DictionaryCache(self.store.path.parent / "dictionary-cache.json")
        self._dictionary_cache: dict[str, DictionaryEntry] = dict(self.dictionary_cache.entries)
        self._dictionary_request_id = 0
        self._dictionary_last_lookup: tuple[str, QPoint, str] | None = None
        self._dictionary_term = ""
        self._dictionary_started_at = 0.0
        self._dictionary_pending_sources: set[str] = set()
        self._dictionary_replies: dict[QNetworkReply, tuple[str, int, int]] = {}
        stored_fallbacks = self.store.data.get("definition_fallbacks", {})
        self.definition_fallbacks = {
            **default_definition_fallbacks(),
            **(stored_fallbacks if isinstance(stored_fallbacks, dict) else {}),
        }
        self.store.data["definition_fallbacks"] = dict(self.definition_fallbacks)
        self._dictionary_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="lumen-definer"
        )
        self._dictionary_future: Any = None
        self._tlamatini_future: Any = None
        self._deep_definition_started = False
        self._dictionary_context = ""
        self._selection_press_position: QPoint | None = None
        self._selection_dragged = False
        self._selection_candidate = ""
        self._selection_candidate_context = ""
        self._selection_candidate_anchor = QPoint()
        self._selection_capture_id = 0
        self._speed_target_active = False
        self._speed_target_document: SpeedReadingDocument | None = None
        self._speed_target_settings: SpeedReaderSettings | None = None
        self._speed_return_highlight_visible = False
        self._pending_speed_return_highlight: tuple[int, int] | None = None
        self._speed_return_highlight_request_id = 0
        self._last_external_link = ""
        self._last_external_link_at = 0.0

        self.setWindowTitle("Lumen — Book Reader")
        self.resize(1420, 900)
        self.setMinimumSize(940, 650)
        self.setAcceptDrops(True)
        self._build_ui()
        self.dictionary_network = QNetworkAccessManager(self)
        self.definition_card.dismissed.connect(self._cancel_dictionary_lookup)
        self.definition_card.retry_requested.connect(self._retry_dictionary_lookup)
        self.selection_prompt.lookup_requested.connect(self._selection_prompt_activated)
        self.offline_dictionary_ready.connect(self._offline_dictionary_finished)
        self.deep_dictionary_ready.connect(self._deep_dictionary_finished)
        # Warm WordNet away from the UI thread so the first double-click is normally instant.
        self._dictionary_executor.submit(lookup_offline_wordnet_entries, "reader", None, 1)
        self.dictionary_session_timer = QTimer(self)
        self.dictionary_session_timer.setInterval(100)
        self.dictionary_session_timer.timeout.connect(self._update_dictionary_session)
        self.selection_prompt_timer = QTimer(self)
        self.selection_prompt_timer.setSingleShot(True)
        self.selection_prompt_timer.timeout.connect(self._show_selection_prompt)
        self.speed_target_poll_timer = QTimer(self)
        self.speed_target_poll_timer.setInterval(60)
        self.speed_target_poll_timer.timeout.connect(self._take_speed_target_pick)
        self.marks_dialog = MarksManagerDialog(self, self.marks_store)
        self.marks_dialog.open_requested.connect(self._open_global_mark)
        self.marks_dialog.changed.connect(self._populate_bookmarks)
        self.sidebar.setVisible(bool(self.store.data.get("sidebar_visible", True)))
        self._connect_shortcuts()
        self._apply_app_theme()
        self._update_header_responsiveness()
        self._populate_welcome(initial_books or [])
        QApplication.instance().installEventFilter(self)

        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self._poll_scroll)
        self.progress_timer.start()
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(3000)
        self.save_timer.timeout.connect(self.save_state)
        self.save_timer.start()

    def _build_ui(self) -> None:
        shell = QWidget()
        shell.setObjectName("appShell")
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(shell)

        self.header = QFrame()
        self.header.setObjectName("header")
        self.header.setFixedHeight(66)
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(18, 10, 18, 10)
        self.header_layout.setSpacing(10)

        self.sidebar_button = QPushButton("☰")
        self.sidebar_button.setObjectName("iconButton")
        self.sidebar_button.setToolTip("Show or hide the book panel")
        self.sidebar_button.clicked.connect(self._toggle_sidebar)
        self.header_layout.addWidget(self.sidebar_button)

        self.brand = QLabel("LUMEN")
        self.brand.setObjectName("brand")
        self.header_layout.addWidget(self.brand)

        self.library_button = QPushButton("←  MY LIBRARY")
        self.library_button.setObjectName("libraryButton")
        self.library_button.setToolTip("Save your place and return to the bookshelf (Alt+Left)")
        self.library_button.setAccessibleName("Return to the Lumen library")
        self.library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_button.clicked.connect(self.return_to_library)
        self.library_button.hide()
        self.header_layout.addWidget(self.library_button)

        self.header_divider = QFrame()
        self.header_divider.setFrameShape(QFrame.Shape.VLine)
        self.header_divider.setObjectName("headerDivider")
        self.header_layout.addWidget(self.header_divider)

        self.reader_identity = QWidget()
        self.reader_identity.setObjectName("readerIdentity")
        self.reader_identity.setMinimumWidth(0)
        self.reader_identity.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        identity_layout = QVBoxLayout(self.reader_identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(0)

        self.chapter_heading = QLabel("Your reading room")
        self.chapter_heading.setObjectName("chapterHeading")
        # Chapter names and source paths come from the book and can be
        # arbitrarily long.  Both lines are elastic so document metadata can
        # never push the controls to their right on top of each other.
        self.chapter_heading.setMinimumWidth(0)
        self.chapter_heading.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        identity_layout.addWidget(self.chapter_heading)

        source_row = QHBoxLayout()
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.setSpacing(2)
        self.source_path_label = SourcePathLabel()
        source_row.addWidget(self.source_path_label, 1)
        self.copy_source_path_button = QToolButton()
        self.copy_source_path_button.setObjectName("sourcePathCopy")
        self.copy_source_path_button.setText("⧉")
        self.copy_source_path_button.setFixedSize(18, 16)
        self.copy_source_path_button.setToolTip("Copy the original file path")
        self.copy_source_path_button.setAccessibleName("Copy the original file path")
        self.copy_source_path_button.clicked.connect(self._copy_source_path)
        self.copy_source_path_button.hide()
        source_row.addWidget(self.copy_source_path_button)
        identity_layout.addLayout(source_row)
        self.header_layout.addWidget(self.reader_identity, 1)

        self.reader_search_cluster = QFrame()
        self.reader_search_cluster.setObjectName("readerSearchCluster")
        self.reader_search_cluster.setFixedWidth(245)
        search_cluster_layout = QHBoxLayout(self.reader_search_cluster)
        search_cluster_layout.setContentsMargins(1, 1, 1, 1)
        search_cluster_layout.setSpacing(0)

        self.reader_search_edit = QLineEdit()
        self.reader_search_edit.setObjectName("readerSearchEdit")
        self.reader_search_edit.setPlaceholderText("Search from here…")
        self.reader_search_edit.setClearButtonEnabled(True)
        self.reader_search_edit.setFixedWidth(180)
        self.reader_search_edit.setAccessibleName("Search the open book")
        self.reader_search_edit.returnPressed.connect(self.run_reader_search)
        self.reader_search_edit.textChanged.connect(self._reader_search_text_changed)
        search_cluster_layout.addWidget(self.reader_search_edit)

        self.reader_search_button = QPushButton("🔍")
        self.reader_search_button.setObjectName("readerSearchButton")
        self.reader_search_button.setFixedWidth(38)
        self.reader_search_button.setAccessibleName("Find next match")
        self.reader_search_button.clicked.connect(self.run_reader_search)
        search_cluster_layout.addWidget(self.reader_search_button)

        self.reader_search_options = QToolButton()
        self.reader_search_options.setObjectName("readerSearchOptions")
        self.reader_search_options.setText("▾")
        self.reader_search_options.setFixedWidth(25)
        self.reader_search_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.reader_search_options.setAccessibleName("Search scope and direction")
        search_cluster_layout.addWidget(self.reader_search_options)
        self._build_reader_search_menu()
        self.reader_search_cluster.hide()
        self.header_layout.addWidget(self.reader_search_cluster)

        self.smaller_button = QPushButton("A−")
        self.smaller_button.setObjectName("compactButton")
        self.smaller_button.setToolTip("Decrease text size")
        self.smaller_button.clicked.connect(lambda: self._change_font_size(-1))
        self.header_layout.addWidget(self.smaller_button)
        self.larger_button = QPushButton("A+")
        self.larger_button.setObjectName("compactButton")
        self.larger_button.setToolTip("Increase text size")
        self.larger_button.clicked.connect(lambda: self._change_font_size(1))
        self.header_layout.addWidget(self.larger_button)

        self.theme_combo = WheelSafeComboBox()
        self.theme_combo.addItems(THEME_NAMES)
        current_theme = str(self.store.data.get("theme", "dark"))
        self.theme_combo.setCurrentText(THEME_LABELS.get(current_theme, "Night"))
        self.theme_combo.currentTextChanged.connect(self._theme_changed)
        self.header_layout.addWidget(self.theme_combo)

        self.speed_reader_button = QPushButton("⚡  Speed")
        self.speed_reader_button.setObjectName("speedReaderButton")
        self.speed_reader_button.setToolTip(
            "Open the configurable rapid serial speed reader (Ctrl+Shift+R)"
        )
        self.speed_reader_button.setAccessibleName("Configure and start speed reading")
        self.speed_reader_button.clicked.connect(self.show_speed_reader)
        self.speed_reader_button.hide()
        self.header_layout.addWidget(self.speed_reader_button)

        self.configure_button = QPushButton("⚙  Configuration")
        self.configure_button.setObjectName("toolButton")
        self.configure_button.setToolTip(
            "The library folder, the sweep engine, the index, search and reading — "
            "every Lumen setting in one window (Ctrl+,)"
        )
        self.configure_button.setAccessibleName("Open Lumen configuration")
        self.configure_button.clicked.connect(self.show_configuration)
        self.header_layout.addWidget(self.configure_button)

        self.definer_button = QPushButton("◇  Definer")
        self.definer_button.setObjectName("toolButton")
        self.definer_button.setToolTip(
            "Configure contextual analysis, Tlamatini Googler, and Ollama definition fallbacks"
        )
        self.definer_button.clicked.connect(self.show_definition_settings)
        self.header_layout.addWidget(self.definer_button)

        self.all_marks_button = QPushButton("Notes & Marks")
        self.all_marks_button.setObjectName("toolButton")
        self.all_marks_button.setToolTip("Search notes and marks from every book (Ctrl+Shift+M)")
        self.all_marks_button.clicked.connect(self.show_all_marks)
        self.header_layout.addWidget(self.all_marks_button)

        self.mark_button = QPushButton("✦  Mark position")
        self.mark_button.setObjectName("toolButton")
        self.mark_button.setToolTip("Mark this position and optionally add a note (Ctrl+B)")
        self.mark_button.setEnabled(False)
        self.mark_button.clicked.connect(self.request_mark_position)
        self.header_layout.addWidget(self.mark_button)

        self.open_book_button = QPushButton("Open Book")
        self.open_book_button.setObjectName("primarySmallButton")
        self.open_book_button.clicked.connect(self.browse_for_book)
        self.header_layout.addWidget(self.open_book_button)
        # Collapse only lower-priority actions when space runs out, and restore
        # them automatically when the window grows again.  This is preferable
        # to letting Qt overlap adjacent controls.
        self._optional_header_widgets = (
            self.open_book_button,
            self.mark_button,
            self.all_marks_button,
            self.definer_button,
            self.configure_button,
        )
        outer.addWidget(self.header)

        self.main_stack = QStackedWidget()
        self.welcome = LibraryShelf(self.library_index, self.library_root)
        self.welcome.browse_requested.connect(self.browse_for_book)
        self.welcome.book_requested.connect(self.open_book)
        self.welcome.rescan_requested.connect(self.rescan_library)
        self.welcome.configure_requested.connect(self.show_configuration)
        self.welcome.apply_search_settings(dict(self.store.data.get("search") or {}))
        # The shelf is added directly - never inside a QScrollArea.  A scroll
        # area would stretch the list to its full content height, materialising
        # every row and undoing the virtualization that makes a huge library
        # instant.  The QListView does its own scrolling.
        self.welcome.view.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(
            self.welcome.view.viewport(), QScroller.ScrollerGestureType.TouchGesture
        )
        self.main_stack.addWidget(self.welcome)

        reader_shell = QWidget()
        reader_layout = QVBoxLayout(reader_shell)
        reader_layout.setContentsMargins(0, 0, 0, 0)
        reader_layout.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        self.sidebar = self._build_sidebar()
        self.splitter.addWidget(self.sidebar)

        self.web = QWebEngineView()
        self.web.setObjectName("bookView")
        self.web.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        page = ReaderWebPage(self.web)
        self.web.setPage(page)
        # acceptNavigationRequest() runs inside QtWebEngine's navigation stack.
        # Loading another chapter synchronously from that callback re-enters
        # Chromium and can terminate the process on Windows. A queued connection
        # lets the callback return before the replacement document is loaded.
        page.link_clicked.connect(self._handle_link, Qt.ConnectionType.QueuedConnection)
        page.newWindowRequested.connect(self._handle_new_window_request)
        settings = self.web.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        self.web.loadFinished.connect(self._chapter_loaded)
        self.splitter.addWidget(self.web)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([330, 1090])
        reader_layout.addWidget(self.splitter, 1)
        reader_layout.addWidget(self._build_footer())
        self.main_stack.addWidget(reader_shell)
        outer.addWidget(self.main_stack, 1)
        self.definition_card = DefinitionCard(shell)
        self.selection_prompt = SelectionLookupPrompt(shell)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(275)
        sidebar.setMaximumWidth(440)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 14)
        layout.setSpacing(10)

        book_header = QHBoxLayout()
        self.cover = QLabel("EPUB")
        self.cover.setObjectName("cover")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedSize(76, 112)
        self.cover.setScaledContents(False)
        book_header.addWidget(self.cover)
        metadata_layout = QVBoxLayout()
        metadata_layout.setSpacing(5)
        self.book_title = QLabel("No book open")
        self.book_title.setObjectName("bookTitle")
        self.book_title.setWordWrap(True)
        metadata_layout.addWidget(self.book_title)
        self.book_author = QLabel("")
        self.book_author.setObjectName("bookAuthor")
        self.book_author.setWordWrap(True)
        metadata_layout.addWidget(self.book_author)
        metadata_layout.addStretch()
        book_header.addLayout(metadata_layout, 1)
        layout.addLayout(book_header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.toc_tree = QTreeWidget()
        self.toc_tree.setObjectName("tocTree")
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.setIndentation(16)
        self.toc_tree.itemActivated.connect(self._toc_activated)
        _enable_precision_scrolling(self.toc_tree)
        self.tabs.addTab(self.toc_tree, "Contents")

        search_page = QWidget()
        search_layout = QVBoxLayout(search_page)
        search_layout.setContentsMargins(0, 8, 0, 0)
        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search in this book…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self.run_search)
        search_row.addWidget(self.search_box, 1)
        search_button = QPushButton("Find")
        search_button.setObjectName("compactButton")
        search_button.clicked.connect(self.run_search)
        search_row.addWidget(search_button)
        search_layout.addLayout(search_row)
        self.search_results = QListWidget()
        self.search_results.setWordWrap(True)
        self.search_results.setSpacing(4)
        self.search_results.itemActivated.connect(self._search_activated)
        _enable_precision_scrolling(self.search_results)
        search_layout.addWidget(self.search_results)
        self.tabs.addTab(search_page, "Search")

        marks_page = QWidget()
        marks_layout = QVBoxLayout(marks_page)
        marks_layout.setContentsMargins(0, 8, 0, 0)
        self.bookmark_list = QListWidget()
        self.bookmark_list.setWordWrap(True)
        self.bookmark_list.setSpacing(5)
        self.bookmark_list.itemActivated.connect(self._bookmark_activated)
        _enable_precision_scrolling(self.bookmark_list)
        marks_layout.addWidget(self.bookmark_list)
        remove_mark = QPushButton("Delete selected")
        remove_mark.setObjectName("subtleButton")
        remove_mark.clicked.connect(self.remove_bookmark)
        marks_layout.addWidget(remove_mark)
        self.tabs.addTab(marks_page, "Notes")
        layout.addWidget(self.tabs, 1)
        return sidebar

    def _build_reader_search_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("readerSearchMenu")
        menu.addSection("SEARCH SCOPE")
        scope_group = QActionGroup(menu)
        scope_group.setExclusive(True)
        self.search_current_page_action = QAction("Current page only", scope_group)
        self.search_current_page_action.setCheckable(True)
        self.search_entire_book_action = QAction("Entire book from here", scope_group)
        self.search_entire_book_action.setCheckable(True)
        self.search_entire_book_action.setChecked(True)
        menu.addActions(scope_group.actions())
        menu.addSeparator()
        menu.addSection("DIRECTION")
        direction_group = QActionGroup(menu)
        direction_group.setExclusive(True)
        self.search_forward_action = QAction("Forward", direction_group)
        self.search_forward_action.setCheckable(True)
        self.search_forward_action.setChecked(True)
        self.search_backward_action = QAction("Backward", direction_group)
        self.search_backward_action.setCheckable(True)
        menu.addActions(direction_group.actions())
        self.search_current_page_action.triggered.connect(
            lambda checked: checked and self._set_reader_search_options(scope="page")
        )
        self.search_entire_book_action.triggered.connect(
            lambda checked: checked and self._set_reader_search_options(scope="book")
        )
        self.search_forward_action.triggered.connect(
            lambda checked: checked and self._set_reader_search_options(backward=False)
        )
        self.search_backward_action.triggered.connect(
            lambda checked: checked and self._set_reader_search_options(backward=True)
        )
        self.reader_search_options.setMenu(menu)
        self._update_reader_search_hint()

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        footer.setFixedHeight(58)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 9, 18, 9)
        layout.setSpacing(12)
        self.previous_button = QPushButton("‹  Previous")
        self.previous_button.setObjectName("subtleButton")
        self.previous_button.clicked.connect(self.previous_chapter)
        layout.addWidget(self.previous_button)
        self.chapter_counter = QLabel("Section 0 of 0")
        self.chapter_counter.setObjectName("footerLabel")
        layout.addWidget(self.chapter_counter)
        self.progress_slider = QSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.sliderPressed.connect(self._slider_pressed)
        self.progress_slider.sliderReleased.connect(self._slider_released)
        layout.addWidget(self.progress_slider, 1)
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("footerLabel")
        self.percent_label.setMinimumWidth(46)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.percent_label)
        self.next_button = QPushButton("Next  ›")
        self.next_button.setObjectName("subtleButton")
        self.next_button.clicked.connect(self.next_chapter)
        layout.addWidget(self.next_button)
        return footer

    def _connect_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Open, self, activated=self.browse_for_book)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.focus_search)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self, activated=self.show_speed_reader)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.show_configuration)
        QShortcut(QKeySequence("F5"), self, activated=self.rescan_library)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.request_mark_position)
        QShortcut(QKeySequence("Ctrl+Shift+M"), self, activated=self.show_all_marks)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.return_to_library)
        QShortcut(QKeySequence("Ctrl+Right"), self, activated=self.next_chapter)
        QShortcut(QKeySequence("Ctrl+Left"), self, activated=self.previous_chapter)
        QShortcut(QKeySequence(Qt.Key.Key_PageDown), self, activated=self.next_chapter)
        QShortcut(QKeySequence(Qt.Key.Key_PageUp), self, activated=self.previous_chapter)
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self._reset_font_size)
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self._change_font_size(1))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self._change_font_size(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._dismiss_definition)

    # ─────────────────────────── the library sweep ─────────────────────────

    def scan_config(self) -> ScanConfig:
        """The sweep settings as the configuration window last left them."""
        stored = self.store.data.get("scan")
        config = ScanConfig.from_mapping(stored)
        if not isinstance(stored, dict) or "text_budget" not in stored:
            # Nothing configured yet.  The text budget lived at the top level
            # long before this window existed, so an upgrading reader keeps the
            # value they already had rather than being silently reset.
            config.text_budget = int(self.store.data.get("text_budget", DEFAULT_TEXT_BUDGET))
        return config

    def rescan_library(self, with_text: bool | None = None) -> None:
        """Sweep the library folder with the full fleet, and show it happening.

        Every path out of here is visible.  A sweep that is already running says
        so; a library folder that does not exist says so and offers to fix
        itself; a sweep that starts opens the monitor.  The one thing that can
        no longer happen is the button appearing to do nothing.
        """
        if self._scanner is not None and self._scanner.is_running():
            if self._monitor is not None:
                self._monitor.show()
                self._monitor.raise_()
                self._monitor.activateWindow()
            return

        root = Path(self.library_root)
        if not root.is_dir():
            answer = QMessageBox.question(
                self, "Where are your books?",
                f"Lumen's library folder is set to:\n\n{root}\n\n"
                f"That folder does not exist, so a sweep would find nothing.\n\n"
                f"Open Configuration to choose the right folder?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.show_configuration()
            return

        config = self.scan_config()
        if with_text is not None:
            config.with_text = bool(with_text)

        scanner = TurboScanner(self.library_index.path, self.library_root, config)
        self._scanner = scanner
        scanner.start()

        monitor = ScanMonitorDialog(scanner, self, self.theme_colors or None)
        monitor.finished_scan.connect(self._sweep_finished)
        self._monitor = monitor
        monitor.show()

        # The shelf footer mirrors the sweep too, so closing the monitor does not
        # hide the fact that the fleet is still working.
        timer = QTimer(self)
        timer.setInterval(400)
        timer.timeout.connect(self._poll_sweep)
        self._sweep_timer = timer
        timer.start()
        self._poll_sweep()

    def _poll_sweep(self) -> None:
        scanner = self._scanner
        if scanner is None:
            return
        state = scanner.snapshot()
        self.welcome.show_sweep(state)
        if not state.running:
            self._sweep_finished(state)

    def _sweep_finished(self, state: object) -> None:
        if self._sweep_timer is not None:
            self._sweep_timer.stop()
            self._sweep_timer = None
        self.welcome.show_sweep(state)
        self.welcome.finish_sweep()
        self._scanner = None

    def start_library_scan_if_needed(self) -> None:
        """Sweep on first run, and whenever the shelf has nothing to show."""
        if not self.scan_config().scan_on_startup:
            return
        try:
            known = self.library_index.counts(self.library_root).total
        except Exception:
            known = 0
        if known == 0 and Path(self.library_root).is_dir():
            self.rescan_library()

    # ────────────────────────── the configuration ──────────────────────────

    def show_configuration(self) -> None:
        """Open the settings window and apply whatever comes back."""
        dialog = ConfigurationDialog(
            self, self.store, self.library_index, self.library_root, self.theme_colors or None
        )
        dialog.index_changed.connect(self.welcome.refresh_counts)
        wants_sweep: list[bool] = []
        dialog.sweep_requested.connect(lambda: wants_sweep.append(True))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        chosen = normalize_root(dialog.chosen_root) if dialog.chosen_root else self.library_root
        if chosen != self.library_root:
            self.change_library_root(dialog.chosen_root)

        self.welcome.apply_search_settings(dict(self.store.data.get("search") or {}))
        theme = str(self.store.data.get("theme", "dark"))
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentText(THEME_LABELS.get(theme, "Night"))
        self.theme_combo.blockSignals(False)
        self._apply_app_theme()
        self._rerender_current()
        self.sidebar.setVisible(bool(self.store.data.get("sidebar_visible", True)))
        self.welcome.refresh_counts()
        if wants_sweep:
            self.rescan_library()

    def change_library_root(self, root: str | Path) -> None:
        """Point Lumen at a different library folder, everywhere at once.

        The marks file lives beside the books by design, so it travels with the
        library rather than staying behind pointing at the old shelf.
        """
        resolved = Path(root).expanduser()
        self.library_root = normalize_root(resolved)
        self.welcome.set_root(self.library_root)
        self.marks_store = MarksStore(resolved / MARKS_FILENAME)
        self.marks_dialog.store = self.marks_store
        self.store.relink_missing_books(resolved)
        self._populate_bookmarks()
        self._populate_welcome([])

    def _populate_welcome(self, initial_books: list[Path]) -> None:
        recent = list(self.store.data.get("recent_books", []))
        known_paths = {str(Path(item.get("path", "")).resolve()) for item in recent if item.get("path")}
        for path in initial_books:
            resolved = str(path.resolve())
            if resolved not in known_paths:
                recent.append(
                    {
                        "path": resolved,
                        "title": path.stem,
                        "author": generic_document_label(path),
                    }
                )
        self.welcome.set_books(recent)

    def browse_for_book(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open a Book",
            str(Path.cwd()),
            "Books (*.epub *.pdf);;EPUB books (*.epub);;PDF documents (*.pdf)",
        )
        if path:
            self.open_book(path)

    def open_book(self, path: str) -> None:
        if self._speed_target_active:
            self._cancel_speed_start_target()
        try:
            new_book = self._load_book(path)
            if new_book is None:
                return
        except (EpubError, PdfError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open book", str(exc))
            return

        old_book = self.book
        self.book = new_book
        if old_book:
            old_book.close()
        self.store.remember_book(str(new_book.path), new_book.metadata.title, new_book.metadata.author_line)
        state = self.store.book_state(new_book.key)
        self.chapter_index = max(0, min(int(state.get("chapter", 0)), len(new_book.chapters) - 1))
        self.pending_scroll = max(0.0, min(float(state.get("scroll", 0.0)), 1.0))
        self.scroll_percent = self.pending_scroll
        self._populate_book_panel()
        self.reader_search_edit.clear()
        self._reset_reader_search(clear_selection=True)
        self._migrate_legacy_bookmarks()
        self._populate_bookmarks()
        self._enter_reading_mode()
        self.show_chapter(self.chapter_index, self.pending_scroll)
        self._populate_welcome(library_books(Path.cwd()))
        self.store.save()

    def _load_book(self, path: str) -> EpubBook | PdfBook | None:
        """Open a supported document, prompting securely for PDF passwords."""
        source = Path(path).expanduser()
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_BOOK_SUFFIXES:
            raise ValueError("Lumen can open EPUB and PDF files.")
        if suffix == ".epub":
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                return EpubBook(source)
            finally:
                QApplication.restoreOverrideCursor()

        password = ""
        first_prompt = True
        while True:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                return PdfBook(source, password=password)
            except PdfPasswordRequired:
                pass
            finally:
                QApplication.restoreOverrideCursor()
            prompt = (
                "This PDF is protected. Enter its password:"
                if first_prompt
                else "That password was not accepted. Try again:"
            )
            password, accepted = QInputDialog.getText(
                self,
                "PDF password required",
                prompt,
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return None
            first_prompt = False

    def _enter_reading_mode(self) -> None:
        if not self.book:
            return
        self.main_stack.setCurrentIndex(1)
        self.library_button.show()
        self.reader_search_cluster.show()
        self.speed_reader_button.show()
        self.mark_button.setEnabled(True)
        self._set_source_path(self.book.path)
        self._update_header_responsiveness()
        self.setWindowTitle(f"{self.book.metadata.title} — Lumen")

    def _set_source_path(self, path: str | Path | None) -> None:
        """Expose the original file identity in the reader chrome."""

        self.source_path_label.set_source_path(path)
        has_path = bool(self.source_path_label.source_path)
        self.copy_source_path_button.setVisible(has_path)
        self.copy_source_path_button.setEnabled(has_path)
        self._reset_source_copy_button()

    def _copy_source_path(self) -> None:
        path = self.source_path_label.source_path
        if not path:
            return
        QApplication.clipboard().setText(path)
        self.copy_source_path_button.setText("✓")
        self.copy_source_path_button.setToolTip("Original file path copied")
        QTimer.singleShot(1100, self._reset_source_copy_button)

    def _reset_source_copy_button(self) -> None:
        self.copy_source_path_button.setText("⧉")
        self.copy_source_path_button.setToolTip("Copy the original file path")

    def return_to_library(self) -> None:
        """Capture the latest scroll offset, save it, and reveal the main bookshelf."""
        if not self.book or self.main_stack.currentIndex() == 0:
            return
        script = (
            "(() => { const max = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);"
            " return max ? window.scrollY / max : 0; })()"
        )
        self.web.page().runJavaScript(script, self._finish_return_to_library)

    def _finish_return_to_library(self, scroll: Any = None) -> None:
        if isinstance(scroll, (int, float)):
            self.scroll_percent = max(0.0, min(float(scroll), 1.0))
        self.save_state()
        if self._speed_target_active:
            self._cancel_speed_start_target()
        self._dismiss_speed_return_highlight()
        self._clear_selection_candidate()
        self._dismiss_definition()
        self.main_stack.setCurrentIndex(0)
        self.library_button.hide()
        self.reader_search_cluster.hide()
        self.speed_reader_button.hide()
        self.mark_button.setEnabled(False)
        self.chapter_heading.setText("Your reading room")
        self._set_source_path(None)
        self._update_header_responsiveness()
        self.setWindowTitle("Lumen — Book Reader")
        self._populate_welcome(library_books(Path.cwd()))

    def _populate_book_panel(self) -> None:
        if not self.book:
            return
        self.book_title.setText(self.book.metadata.title)
        self.book_author.setText(self.book.metadata.author_line)
        document_type = getattr(self.book, "document_type", "EPUB")
        text_controls_enabled = document_type == "EPUB"
        self.smaller_button.setEnabled(text_controls_enabled)
        self.larger_button.setEnabled(text_controls_enabled)
        control_hint = (
            "Decrease text size"
            if text_controls_enabled
            else "PDF pages retain their original typography"
        )
        self.smaller_button.setToolTip(control_hint)
        self.larger_button.setToolTip(
            "Increase text size"
            if text_controls_enabled
            else "PDF pages retain their original typography"
        )
        cover_path = self.book.cover_path
        if cover_path:
            pixmap = QPixmap(str(cover_path))
            if not pixmap.isNull():
                self.cover.setPixmap(
                    pixmap.scaled(
                        self.cover.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.cover.setText(document_type)
        else:
            self.cover.setText(document_type)
        self._building_toc = True
        self.toc_tree.clear()

        def add_entries(entries: list[TocEntry], parent: QTreeWidgetItem | None = None) -> None:
            for entry in entries:
                item = QTreeWidgetItem([entry.title])
                item.setData(0, Qt.ItemDataRole.UserRole, entry.chapter_index)
                if parent is None:
                    self.toc_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                add_entries(entry.children, item)

        add_entries(self.book.toc)
        self.toc_tree.expandToDepth(0)
        self._building_toc = False

    def show_chapter(
        self,
        index: int,
        scroll: float = 0.0,
        find_text: str = "",
        find_backward: bool = False,
        rsvp_return_highlight: tuple[int, int] | None = None,
    ) -> None:
        if not self.book or not (0 <= index < len(self.book.chapters)):
            return
        self._dismiss_speed_return_highlight()
        self._pending_speed_return_highlight = rsvp_return_highlight
        self._clear_selection_candidate()
        self._dismiss_definition()
        if index != self.chapter_index:
            self._remember_progress()
        self.chapter_index = index
        self.pending_scroll = max(0.0, min(scroll, 1.0))
        self.pending_find = find_text
        self.pending_find_backward = find_backward
        chapter = self.book.chapters[index]
        theme = str(self.store.data.get("theme", "dark"))
        font_size = int(self.store.data.get("font_size", 20))
        try:
            rendered = self.book.chapter_html(index, theme, font_size)
        except (EpubError, PdfError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not render this page", str(exc))
            return
        self.web.setHtml(rendered, QUrl(self.book.chapter_base_url(index)))
        self.chapter_heading.setText(chapter.title)
        unit = "Page" if getattr(self.book, "document_type", "EPUB") == "PDF" else "Section"
        self.chapter_counter.setText(f"{unit} {index + 1} of {len(self.book.chapters)}")
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(self.book.chapters) - 1)
        self._select_toc_chapter(index)
        self._update_overall_progress(self.pending_scroll)

    def _chapter_loaded(self, ok: bool) -> None:
        if not ok:
            return
        # Install the reader-owned guard before restoring the saved position.
        # It prevents link focus/click side effects while preserving normal
        # text selection, including selections that begin on linked text.
        self.web.page().runJavaScript(READER_INTERACTION_GUARD_SCRIPT)
        if self._speed_target_active:
            self.web.page().runJavaScript(RSVP_TARGETING_SCRIPT)
        percent = self.pending_scroll
        script = (
            "(() => { const max = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);"
            f" window.scrollTo(0, max * {percent!r}); return max; }})()"
        )
        self.web.page().runJavaScript(script)
        if self._pending_speed_return_highlight is not None:
            word_index, word_count = self._pending_speed_return_highlight
            self._pending_speed_return_highlight = None
            self._speed_return_highlight_request_id += 1
            request_id = self._speed_return_highlight_request_id
            self.web.page().runJavaScript(
                rsvp_return_highlight_script(word_index, word_count),
                lambda payload, serial=request_id: self._speed_return_highlight_installed(
                    payload, serial
                ),
            )
        if self.pending_find:
            find_text = json.dumps(self.pending_find)
            find_backward = "true" if self.pending_find_backward else "false"
            QTimer.singleShot(
                120,
                lambda: self.web.page().runJavaScript(
                    f"window.find({find_text}, false, {find_backward}, true);"
                ),
            )
        self.pending_find = ""
        self.pending_find_backward = False

    def _speed_return_highlight_installed(self, payload: Any, request_id: int) -> None:
        if request_id != self._speed_return_highlight_request_id:
            return
        try:
            data = json.loads(payload) if isinstance(payload, str) else {}
        except json.JSONDecodeError:
            data = {}
        self._speed_return_highlight_visible = bool(data.get("found"))

    def _dismiss_speed_return_highlight(self) -> None:
        """Remove the transient red marker without changing reading progress."""
        self._speed_return_highlight_request_id += 1
        self._pending_speed_return_highlight = None
        if hasattr(self, "web"):
            self.web.page().runJavaScript(RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT)
        self._speed_return_highlight_visible = False

    def next_chapter(self) -> None:
        if self.book and self.chapter_index + 1 < len(self.book.chapters):
            self.show_chapter(self.chapter_index + 1)

    def previous_chapter(self) -> None:
        if self.book and self.chapter_index > 0:
            self.show_chapter(self.chapter_index - 1)

    def _toc_activated(self, item: QTreeWidgetItem) -> None:
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if index is not None:
            self.show_chapter(int(index))

    def _select_toc_chapter(self, index: int) -> None:
        iterator = self.toc_tree.invisibleRootItem()
        stack = [iterator.child(i) for i in range(iterator.childCount())]
        while stack:
            item = stack.pop(0)
            if item.data(0, Qt.ItemDataRole.UserRole) == index:
                self.toc_tree.setCurrentItem(item)
                return
            stack[0:0] = [item.child(i) for i in range(item.childCount())]

    def focus_search(self) -> None:
        if not self.book:
            return
        self.reader_search_edit.setFocus()
        self.reader_search_edit.selectAll()

    def show_speed_reader(self) -> None:
        """Configure RSVP, then let the reader point at its exact first word."""
        if self._speed_target_active:
            self._cancel_speed_start_target()
            return
        if not self.book or self.main_stack.currentIndex() == 0:
            return
        settings = SpeedReaderSettings.from_mapping(self.store.data.get("speed_reader"))
        setup = SpeedReaderSettingsDialog(settings, self)
        if setup.exec() != QDialog.DialogCode.Accepted:
            return
        settings = setup.settings
        self.store.data["speed_reader"] = settings.to_dict()
        self.store.save()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            document = SpeedReadingDocument.from_book(self.book)
        except (EpubError, PdfError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not start speed reading", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        if not document.total_words:
            QMessageBox.information(
                self,
                "No readable text",
                "This book has no extractable text for the speed reader. "
                "Image-only PDFs require OCR before their words can be presented.",
            )
            return

        self._begin_speed_start_target(document, settings)

    def _begin_speed_start_target(
        self, document: SpeedReadingDocument, settings: SpeedReaderSettings
    ) -> None:
        """Turn the live book page into a precise, cursor-driven RSVP launcher."""
        self._clear_selection_candidate()
        self._dismiss_definition()
        self._speed_target_document = document
        self._speed_target_settings = settings
        self._speed_target_active = True
        self.speed_reader_button.setText("✕  Cancel")
        self.speed_reader_button.setToolTip("Cancel choosing the RSVP starting word (Esc)")
        self.speed_reader_button.setAccessibleName("Cancel RSVP starting-word selection")
        self.web.page().runJavaScript(RSVP_TARGETING_SCRIPT)
        self.speed_target_poll_timer.start()
        self.web.setFocus()

    def _cancel_speed_start_target(self) -> None:
        """Leave targeting mode without changing the reader's place."""
        self.speed_target_poll_timer.stop()
        if hasattr(self, "web"):
            self.web.page().runJavaScript(RSVP_TARGET_STOP_SCRIPT)
        self._speed_target_active = False
        self._speed_target_document = None
        self._speed_target_settings = None
        self.speed_reader_button.setText("⚡  Speed")
        self.speed_reader_button.setToolTip(
            "Open the configurable rapid serial speed reader (Ctrl+Shift+R)"
        )
        self.speed_reader_button.setAccessibleName("Configure and start speed reading")

    def _take_speed_target_pick(self) -> None:
        if not self._speed_target_active:
            return
        self.web.page().runJavaScript(
            RSVP_TARGET_TAKE_PICK_SCRIPT,
            self._speed_target_pick_received,
        )

    def _speed_target_pick_received(self, payload: Any) -> None:
        if not self._speed_target_active or not self.book:
            return
        try:
            data = json.loads(payload) if isinstance(payload, str) else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return
        document = self._speed_target_document
        settings = self._speed_target_settings
        if document is None or settings is None or not (0 <= self.chapter_index < len(document.chapters)):
            self._cancel_speed_start_target()
            return
        words = document.chapters[self.chapter_index].words
        word_index = resolve_rsvp_target_word_index(words, data)
        if word_index is None:
            return

        chapter_index = self.chapter_index
        self._cancel_speed_start_target()
        QTimer.singleShot(
            0,
            lambda doc=document, configured=settings, chapter=chapter_index, word=word_index: self._launch_speed_reader(
                doc, configured, chapter, word
            ),
        )

    def _launch_speed_reader(
        self,
        document: SpeedReadingDocument,
        settings: SpeedReaderSettings,
        chapter_index: int,
        word_index: int,
    ) -> None:
        if not self.book:
            return

        player = SpeedReaderDialog(
            document=document,
            settings=settings,
            book_title=self.book.metadata.title,
            start_chapter=chapter_index,
            start_scroll=0.0,
            start_word_index=word_index,
            parent=self,
        )
        QTimer.singleShot(0, player.start_session)
        if settings.fullscreen:
            player.showFullScreen()
        player.exec()
        last_presented = player.last_presented_position()
        chapter_index, chapter_scroll = player.reading_position()
        # Retain live WPM adjustments made with the arrow keys for the next session.
        self.store.data["speed_reader"] = player.settings.to_dict()
        self.store.save()
        if last_presented is None:
            self.show_chapter(chapter_index, chapter_scroll)
            return
        last_chapter, last_word, last_count = last_presented
        words = document.chapters[last_chapter].words
        exact_scroll = last_word / max(len(words), 1)
        self.show_chapter(
            last_chapter,
            exact_scroll,
            rsvp_return_highlight=(last_word, last_count),
        )

    def _set_reader_search_options(
        self, scope: str | None = None, backward: bool | None = None
    ) -> None:
        if scope is not None:
            self._reader_search_scope = scope
        if backward is not None:
            self._reader_search_backward = backward
        self._reset_reader_search(clear_selection=True)
        self._update_reader_search_hint()

    def _update_reader_search_hint(self) -> None:
        scope = "current page" if self._reader_search_scope == "page" else "entire book"
        direction = "backward" if self._reader_search_backward else "forward"
        hint = f"Find {direction} in the {scope} (Enter)"
        self.reader_search_button.setToolTip(hint)
        self.reader_search_options.setToolTip(
            f"Search scope: {scope}\nDirection: {direction}"
        )

    def _reader_search_text_changed(self, text: str) -> None:
        self._reset_reader_search(clear_selection=not text.strip())

    def _reset_reader_search(self, clear_selection: bool = False) -> None:
        self._reader_search_results = []
        self._reader_search_position = -1
        self._reader_search_occurrence = 0
        self._reader_search_session = None
        self._set_reader_search_state("")
        if clear_selection and hasattr(self, "web"):
            self.web.page().runJavaScript(
                "window.getSelection && window.getSelection().removeAllRanges();"
            )

    def _set_reader_search_state(self, state: str, message: str = "") -> None:
        self.reader_search_cluster.setProperty("searchState", state)
        self.reader_search_cluster.style().unpolish(self.reader_search_cluster)
        self.reader_search_cluster.style().polish(self.reader_search_cluster)
        if message:
            self.reader_search_button.setToolTip(message)
        else:
            self._update_reader_search_hint()

    def _find_on_rendered_page(
        self, query: str, message: str = "", known_match: bool = False
    ) -> None:
        encoded_query = json.dumps(query)
        backward = "true" if self._reader_search_backward else "false"
        self.web.page().runJavaScript(
            f"window.find({encoded_query}, false, {backward}, true);",
            lambda found, searched=query, detail=message, expected=known_match: self._page_find_finished(
                found, searched, detail, expected
            ),
        )

    def _page_find_finished(
        self, found: Any, query: str, message: str, known_match: bool
    ) -> None:
        if query != self.reader_search_edit.text().strip():
            return
        matched = bool(found) or known_match
        if matched:
            self._set_reader_search_state("found", message or "Match found on this page")
        else:
            self._set_reader_search_state("miss", "No matches on this page")

    def run_reader_search(self) -> None:
        if not self.book:
            return
        query = self.reader_search_edit.text().strip()
        if not query:
            self._set_reader_search_state("miss", "Type a word or phrase to search")
            self.reader_search_edit.setFocus()
            return
        if self._reader_search_scope == "page":
            self._find_on_rendered_page(query)
            return
        self._run_reader_book_search(query)

    def _run_reader_book_search(self, query: str) -> None:
        if not self.book:
            return
        session_matches = (
            self._reader_search_session is not None
            and self._reader_search_session[:2] == (query, self._reader_search_backward)
            and 0 <= self._reader_search_position < len(self._reader_search_results)
            and self._reader_search_results[self._reader_search_position].chapter_index
            == self.chapter_index
        )
        if not session_matches:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                results = self.book.search(query, limit=len(self.book.chapters))
            finally:
                QApplication.restoreOverrideCursor()
            self._reader_search_results = search_results_from_page(
                results, self.chapter_index, self._reader_search_backward
            )
            self._reader_search_position = 0 if self._reader_search_results else -1
            self._reader_search_session = (
                query,
                self._reader_search_backward,
                self.chapter_index,
            )
            if not self._reader_search_results:
                self._set_reader_search_state("miss", "No matches in this book")
                return
            result = self._reader_search_results[0]
            self._reader_search_occurrence = (
                result.match_count if self._reader_search_backward else 1
            )
            self._show_reader_search_result(result, query)
            return

        result = self._reader_search_results[self._reader_search_position]
        has_another_on_page = (
            self._reader_search_occurrence > 1
            if self._reader_search_backward
            else self._reader_search_occurrence < result.match_count
        )
        if has_another_on_page:
            self._reader_search_occurrence += -1 if self._reader_search_backward else 1
        else:
            self._reader_search_position = (
                self._reader_search_position + 1
            ) % len(self._reader_search_results)
            result = self._reader_search_results[self._reader_search_position]
            self._reader_search_occurrence = (
                result.match_count if self._reader_search_backward else 1
            )
        self._show_reader_search_result(result, query)

    def _show_reader_search_result(self, result: SearchResult, query: str) -> None:
        if not self.book:
            return
        unit = "page" if getattr(self.book, "document_type", "EPUB") == "PDF" else "section"
        detail = (
            f"Match {self._reader_search_occurrence} of {result.match_count} in {unit} "
            f"{result.chapter_index + 1}; Enter finds the next match"
        )
        self._set_reader_search_state("found", detail)
        if result.chapter_index == self.chapter_index:
            self._find_on_rendered_page(query, detail, known_match=True)
        else:
            self.show_chapter(
                result.chapter_index,
                find_text=query,
                find_backward=self._reader_search_backward,
            )

    def run_search(self) -> None:
        if not self.book:
            return
        query = self.search_box.text().strip()
        self.search_results.clear()
        if not query:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = self.book.search(query)
        finally:
            QApplication.restoreOverrideCursor()
        if not results:
            empty = QListWidgetItem("No matches found")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.search_results.addItem(empty)
            return
        for result in results:
            plural = "match" if result.match_count == 1 else "matches"
            item = QListWidgetItem(
                f"{result.chapter_title}  ·  {result.match_count} {plural}\n{result.excerpt}"
            )
            item.setData(Qt.ItemDataRole.UserRole, result.chapter_index)
            item.setData(Qt.ItemDataRole.UserRole + 1, query)
            self.search_results.addItem(item)

    def _search_activated(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is not None:
            self.show_chapter(int(index), find_text=str(item.data(Qt.ItemDataRole.UserRole + 1) or ""))

    def _capture_drag_selection(self, global_position: QPoint, capture_id: int) -> None:
        if not self.book or capture_id != self._selection_capture_id:
            return
        self.web.page().runJavaScript(
            SELECTION_CONTEXT_SCRIPT,
            lambda payload, point=QPoint(global_position), serial=capture_id: self._drag_selection_payload_received(
                payload, point, serial
            ),
        )

    def _drag_selection_payload_received(
        self, payload: Any, global_position: QPoint, capture_id: int
    ) -> None:
        try:
            data = json.loads(payload) if isinstance(payload, str) else {}
        except json.JSONDecodeError:
            data = {}
        self._drag_selection_received(
            str(data.get("selection") or ""),
            global_position,
            capture_id,
            str(data.get("context") or ""),
        )

    def _drag_selection_received(
        self,
        selection: str,
        global_position: QPoint,
        capture_id: int,
        context: str = "",
    ) -> None:
        if capture_id != self._selection_capture_id:
            return
        term = normalize_lookup_text(selection)
        if term is None:
            self._clear_selection_candidate()
            return
        self.selection_prompt.dismiss()
        self._selection_candidate = term
        self._selection_candidate_context = " ".join(context.split())[:1800]
        self._selection_candidate_anchor = QPoint(global_position)
        self.selection_prompt_timer.start(selection_lookup_delay_ms(term))

    def _show_selection_prompt(self) -> None:
        if self._selection_candidate:
            self.selection_prompt.show_for(
                self._selection_candidate, self._selection_candidate_anchor
            )

    def _selection_prompt_activated(self, selection: str, global_position: QPoint) -> None:
        context = self._selection_candidate_context
        self.selection_prompt_timer.stop()
        self._selection_candidate = ""
        self._selection_candidate_context = ""
        self._selection_capture_id += 1
        self._lookup_selected_word(selection, global_position, context)

    def _clear_selection_candidate(self) -> None:
        self._selection_capture_id += 1
        self.selection_prompt_timer.stop()
        self.selection_prompt.dismiss()
        self._selection_candidate = ""
        self._selection_candidate_context = ""

    def _has_multiword_selection_candidate(self) -> bool:
        return len(self._selection_candidate.split()) > 1

    def _request_definition_at(self, global_position: QPoint) -> None:
        if not self.book:
            return
        self.web.page().runJavaScript(
            SELECTION_CONTEXT_SCRIPT,
            lambda payload, point=QPoint(global_position): self._definition_payload_received(
                payload, point
            ),
        )

    def _definition_payload_received(self, payload: Any, global_position: QPoint) -> None:
        try:
            data = json.loads(payload) if isinstance(payload, str) else {}
        except json.JSONDecodeError:
            data = {}
        self._lookup_selected_word(
            str(data.get("selection") or ""),
            global_position,
            str(data.get("context") or ""),
        )

    def _lookup_selected_word(
        self, selection: str, global_position: QPoint, context: str = ""
    ) -> None:
        term = normalize_lookup_text(selection)
        if not term:
            self._dismiss_definition()
            return
        self._cancel_dictionary_lookup()
        session_id = self._dictionary_request_id
        self._dictionary_term = term
        self._dictionary_context = " ".join(context.split())[:1800]
        self._dictionary_started_at = time.monotonic()
        self._deep_definition_started = False
        self._dictionary_last_lookup = (
            term,
            QPoint(global_position),
            self._dictionary_context,
        )
        self.definition_card.begin_lookup(term, global_position)

        cache_key = term.casefold()
        cached = self._dictionary_cache.get(cache_key) or self.dictionary_cache.get(cache_key)
        if cached is not None:
            self._dictionary_cache[cache_key] = cached
            self.definition_card.add_entry(cached)

        self._dictionary_pending_sources.add("wordnet")
        future = self._dictionary_executor.submit(lookup_offline_wordnet_entries, term, None, 2)
        self._dictionary_future = future

        def deliver_offline_result(
            completed: Any, lookup_term: str = term, serial: int = session_id
        ) -> None:
            try:
                entries = completed.result()
            except Exception:
                entries = []
            try:
                self.offline_dictionary_ready.emit(lookup_term, entries, serial)
            except RuntimeError:
                pass

        future.add_done_callback(deliver_offline_result)

        is_phrase = " " in term
        sources = ["wiktionary"]
        if is_phrase:
            sources.extend(("wikipedia", "datamuse"))
        else:
            sources.append("dictionaryapi")
        self._dictionary_pending_sources.update(sources)
        for source in sources:
            self._start_dictionary_source(source, term, session_id, 1)
        self.dictionary_session_timer.start()
        self._update_dictionary_session()
        # Conventional services receive a short head start. A total miss then
        # activates the expert ladder without wasting the full 20-second window.
        QTimer.singleShot(
            1800,
            lambda lookup_term=term, serial=session_id: self._start_deep_dictionary_fallbacks(
                lookup_term, serial
            ),
        )

    def _dictionary_source_url(self, source: str, term: str) -> str:
        encoded = quote(term, safe="'-" if source == "dictionaryapi" else "")
        if source == "dictionaryapi":
            return f"{DICTIONARY_API_BASE_URL}/{encoded}"
        if source == "wiktionary":
            return f"{WIKTIONARY_API_BASE_URL}/{encoded}"
        if source == "wikipedia":
            return (
                f"{WIKIPEDIA_API_BASE_URL}?action=query&prop=extracts&exintro=1&explaintext=1"
                f"&exchars=900&redirects=1&titles={encoded}&format=json&formatversion=2"
            )
        return f"{DATAMUSE_API_BASE_URL}?ml={encoded}&md=dp&max=5"

    def _start_dictionary_source(
        self, source: str, term: str, request_id: int, attempt: int
    ) -> None:
        if (
            request_id != self._dictionary_request_id
            or source not in self._dictionary_pending_sources
            or self._dictionary_seconds_remaining() <= 0
        ):
            return
        request = QNetworkRequest(QUrl(self._dictionary_source_url(source, term)))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Lumen Book Reader/1.1")
        request.setTransferTimeout(min(6000, max(1200, int(self._dictionary_seconds_remaining() * 1000))))
        reply = self.dictionary_network.get(request)
        self._dictionary_replies[reply] = (source, request_id, attempt)
        reply.finished.connect(
            lambda current_reply=reply, lookup_term=term: self._dictionary_source_finished(
                current_reply, lookup_term
            )
        )

    def _offline_dictionary_finished(
        self, term: str, entries: list[DictionaryEntry], request_id: int
    ) -> None:
        if request_id != self._dictionary_request_id:
            return
        self._dictionary_future = None
        self._dictionary_pending_sources.discard("wordnet")
        self._append_dictionary_entries(term, entries, "wordnet")
        self._maybe_finish_dictionary_session()

    def _dictionary_source_finished(self, reply: QNetworkReply, term: str) -> None:
        context = self._dictionary_replies.pop(reply, None)
        if context is None:
            reply.deleteLater()
            return
        source, request_id, attempt = context
        if request_id != self._dictionary_request_id:
            reply.deleteLater()
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        payload = bytes(reply.readAll()) if error == QNetworkReply.NetworkError.NoError else b""
        reply.deleteLater()

        if error != QNetworkReply.NetworkError.NoError:
            if source == "ollama":
                self._dictionary_pending_sources.discard(source)
                self._maybe_finish_dictionary_session()
                return
            if status == 404:
                self._dictionary_pending_sources.discard(source)
                self._maybe_finish_dictionary_session()
                return
            retry_index = min(attempt - 1, len(DICTIONARY_RETRY_DELAYS_MS) - 1)
            delay = DICTIONARY_RETRY_DELAYS_MS[retry_index]
            if self._dictionary_seconds_remaining() > (delay / 1000) + 0.8:
                QTimer.singleShot(
                    delay,
                    lambda current_source=source, lookup_term=term, serial=request_id, next_attempt=attempt + 1: self._start_dictionary_source(
                        current_source, lookup_term, serial, next_attempt
                    ),
                )
            return

        entries: list[DictionaryEntry] = []
        if source == "dictionaryapi":
            entries = parse_dictionary_entries(payload)
        elif source == "wiktionary":
            entries = parse_wiktionary_entries(payload, term)
        elif source == "wikipedia":
            entry = parse_wikipedia_phrase(payload, term)
            entries = [entry] if entry else []
        elif source == "datamuse":
            entry = parse_datamuse_phrase(payload, term)
            entries = [entry] if entry else []
        elif source == "ollama":
            model = str(self.definition_fallbacks.get("ollama_model") or DEFAULT_OLLAMA_MODEL)
            entries = parse_ollama_chat_response(payload, term, model)
        self._dictionary_pending_sources.discard(source)
        self._append_dictionary_entries(term, entries, source)
        self._maybe_finish_dictionary_session()

    def _append_dictionary_entries(
        self, term: str, entries: list[DictionaryEntry], source: str
    ) -> None:
        added: list[DictionaryEntry] = []
        for entry in entries:
            if self.definition_card.add_entry(entry):
                added.append(entry)
        if not added:
            return
        cache_key = term.casefold()
        current = self.dictionary_cache.get(cache_key)
        cacheable = source in {"dictionaryapi", "wiktionary", "wordnet"}
        if cacheable and (source == "dictionaryapi" or current is None):
            self._dictionary_cache[cache_key] = added[0]
            self.dictionary_cache.put(cache_key, added[0])

    def _start_deep_dictionary_fallbacks(self, term: str, request_id: int) -> None:
        if (
            request_id != self._dictionary_request_id
            or not self._dictionary_started_at
            or self._deep_definition_started
            or self.definition_card.definition_count
        ):
            return
        self._deep_definition_started = True
        settings = self.definition_fallbacks

        if bool(settings.get("contextual_inference", True)):
            inferred = infer_contextual_entries(term, self._dictionary_context)
            self._append_dictionary_entries(term, inferred, "contextual")

        googler_path = Path(
            str(settings.get("googler_path") or DEFAULT_GOOGLER_PATH)
        )
        if bool(settings.get("googler_enabled")) and googler_path.joinpath("googler.py").is_file():
            self._dictionary_pending_sources.add("googler")
            timeout = min(12.0, max(2.0, self._dictionary_seconds_remaining() - 1.0))
            future = self._dictionary_executor.submit(
                run_tlamatini_googler, term, googler_path, timeout
            )
            self._tlamatini_future = future

            def deliver_googler_result(
                completed: Any, lookup_term: str = term, serial: int = request_id
            ) -> None:
                try:
                    entries = completed.result()
                except Exception:
                    entries = []
                try:
                    self.deep_dictionary_ready.emit(
                        lookup_term, entries, "googler", serial
                    )
                except RuntimeError:
                    pass

            future.add_done_callback(deliver_googler_result)

        if bool(settings.get("ollama_enabled")):
            model = str(settings.get("ollama_model") or DEFAULT_OLLAMA_MODEL).strip()
            if model:
                self._dictionary_pending_sources.add("ollama")
                self._start_ollama_source(term, request_id, model)

        self._maybe_finish_dictionary_session()

    def _start_ollama_source(self, term: str, request_id: int, model: str) -> None:
        if (
            request_id != self._dictionary_request_id
            or "ollama" not in self._dictionary_pending_sources
            or self._dictionary_seconds_remaining() <= 0
        ):
            return
        endpoint = normalized_ollama_url(
            str(self.definition_fallbacks.get("ollama_url") or DEFAULT_OLLAMA_URL),
            "chat",
        )
        book_title = self.book.metadata.title if self.book else ""
        chapter_title = (
            self.book.chapters[self.chapter_index].title
            if self.book and 0 <= self.chapter_index < len(self.book.chapters)
            else ""
        )
        payload = build_ollama_chat_payload(
            term,
            self._dictionary_context,
            model,
            book_title=book_title,
            chapter_title=chapter_title,
        )
        request = QNetworkRequest(QUrl(endpoint))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Lumen Book Reader/1.1")
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setTransferTimeout(
            min(16000, max(1500, int(self._dictionary_seconds_remaining() * 1000)))
        )
        body = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        reply = self.dictionary_network.post(request, body)
        self._dictionary_replies[reply] = ("ollama", request_id, 1)
        reply.finished.connect(
            lambda current_reply=reply, lookup_term=term: self._dictionary_source_finished(
                current_reply, lookup_term
            )
        )

    def _deep_dictionary_finished(
        self, term: str, entries: list[DictionaryEntry], source: str, request_id: int
    ) -> None:
        if (
            request_id != self._dictionary_request_id
            or source not in self._dictionary_pending_sources
        ):
            return
        if source == "googler":
            self._tlamatini_future = None
        self._dictionary_pending_sources.discard(source)
        self._append_dictionary_entries(term, entries, source)
        self._maybe_finish_dictionary_session()

    def _dictionary_seconds_remaining(self) -> float:
        if not self._dictionary_started_at:
            return 0.0
        return max(0.0, DICTIONARY_SESSION_SECONDS - (time.monotonic() - self._dictionary_started_at))

    def _update_dictionary_session(self) -> None:
        remaining = self._dictionary_seconds_remaining()
        self.definition_card.set_progress(remaining, len(self._dictionary_pending_sources))
        if remaining <= 0:
            self._finish_dictionary_session()

    def _maybe_finish_dictionary_session(self) -> None:
        if (
            not self._dictionary_pending_sources
            and not self.definition_card.definition_count
            and not self._deep_definition_started
        ):
            self._start_deep_dictionary_fallbacks(
                self._dictionary_term, self._dictionary_request_id
            )
            return
        if self._dictionary_pending_sources:
            self._update_dictionary_session()
            return
        if self.definition_card.definition_count:
            self._finish_dictionary_session()
        else:
            # Keep the elegant countdown alive for the promised full 20-second search window.
            self._update_dictionary_session()

    def _finish_dictionary_session(self) -> None:
        self.dictionary_session_timer.stop()
        for reply in list(self._dictionary_replies):
            if reply.isRunning():
                reply.abort()
            reply.deleteLater()
        self._dictionary_replies.clear()
        self._dictionary_pending_sources.clear()
        count = self.definition_card.definition_count
        if count:
            sources = self.definition_card.source_count
            self.definition_card.finish(
                f"Complete · {count} definition{'s' if count != 1 else ''} from "
                f"{sources} source{'s' if sources != 1 else ''}"
            )
        else:
            self.definition_card.show_error(
                self._dictionary_term,
                "After a complete 20-second search, conventional dictionaries, contextual "
                "analysis, and every enabled expert source returned no usable definition.",
                retryable=True,
            )
        self._dictionary_started_at = 0.0

    def _retry_dictionary_lookup(self) -> None:
        if self._dictionary_last_lookup is None:
            return
        term, position, context = self._dictionary_last_lookup
        self._lookup_selected_word(term, position, context)

    def _cancel_dictionary_lookup(self) -> None:
        self._dictionary_request_id += 1
        if hasattr(self, "dictionary_session_timer"):
            self.dictionary_session_timer.stop()
        future = self._dictionary_future
        self._dictionary_future = None
        if future is not None:
            future.cancel()
        tlamatini_future = self._tlamatini_future
        self._tlamatini_future = None
        if tlamatini_future is not None:
            tlamatini_future.cancel()
        for reply in list(self._dictionary_replies):
            if reply.isRunning():
                reply.abort()
            reply.deleteLater()
        self._dictionary_replies.clear()
        self._dictionary_pending_sources.clear()
        self._dictionary_started_at = 0.0
        self._dictionary_context = ""
        self._deep_definition_started = False

    def _dismiss_definition(self) -> None:
        self.definition_card.hide()
        self.definition_card.animation_timer.stop()
        self._clear_selection_candidate()
        self._cancel_dictionary_lookup()

    def request_mark_position(self) -> None:
        """Capture the live page position and selected passage before showing the note editor."""
        if not self.book:
            return
        script = """
            (() => {
                const max = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);
                const selection = window.getSelection
                    ? (window.__lumenSelectedText
                        ? window.__lumenSelectedText()
                        : window.getSelection().toString())
                    : '';
                return JSON.stringify({scroll: max ? window.scrollY / max : 0, selection});
            })()
        """
        self.web.page().runJavaScript(script, self._mark_context_received)

    def _mark_context_received(self, value: Any) -> None:
        if not self.book:
            return
        try:
            payload = json.loads(value) if isinstance(value, str) else {}
        except json.JSONDecodeError:
            payload = {}
        scroll = max(0.0, min(float(payload.get("scroll", self.scroll_percent)), 1.0))
        quote_text = " ".join(str(payload.get("selection") or "").split())[:1000]
        self.scroll_percent = scroll
        chapter = self.book.chapters[self.chapter_index]
        overall = (self.chapter_index + scroll) / max(len(self.book.chapters), 1)
        dialog = MarkPositionDialog(
            book_title=self.book.metadata.title,
            chapter_title=chapter.title,
            progress_text=(
                f"{round(overall * 100)}% through the book · "
                f"{round(scroll * 100)}% through this section"
            ),
            quote=quote_text,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mark = ReadingMark.create(
            book_path=str(self.book.path),
            book_title=self.book.metadata.title,
            book_author=self.book.metadata.author_line,
            chapter_index=self.chapter_index,
            chapter_title=chapter.title,
            scroll_percent=scroll,
            overall_percent=overall,
            note=dialog.note,
            quote=quote_text,
            tags=dialog.tags,
        )
        self.marks_store.add(mark)
        self._populate_bookmarks()
        self.marks_dialog.refresh()
        self.tabs.setCurrentIndex(2)

    def _populate_bookmarks(self) -> None:
        self.bookmark_list.clear()
        if not self.book:
            return
        marks = self.marks_store.for_book(self.book.path)
        for mark in marks:
            details = mark.summary.replace("\n", " ")
            if len(details) > 92:
                details = details[:89].rstrip() + "…"
            tags = f"  ·  {' '.join('#' + tag for tag in mark.tags)}" if mark.tags else ""
            item = QListWidgetItem(
                f"{mark.chapter_title}  ·  {round(mark.overall_percent * 100)}%\n"
                f"{details}{tags}"
            )
            item.setToolTip(
                f"{mark.book_title}\n{mark.chapter_title}\n"
                f"{round(mark.scroll_percent * 100)}% through this section\n\n{mark.summary}"
            )
            item.setSizeHint(QSize(0, 70))
            item.setData(Qt.ItemDataRole.UserRole, mark.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, mark.chapter_index)
            item.setData(Qt.ItemDataRole.UserRole + 2, mark.scroll_percent)
            self.bookmark_list.addItem(item)
        if not marks:
            empty = QListWidgetItem("No notes or marks yet\nPress Ctrl+B while reading")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmark_list.addItem(empty)

    def _bookmark_activated(self, item: QListWidgetItem) -> None:
        chapter = item.data(Qt.ItemDataRole.UserRole + 1)
        scroll = item.data(Qt.ItemDataRole.UserRole + 2)
        if chapter is not None:
            self.show_chapter(int(chapter), float(scroll or 0.0))

    def remove_bookmark(self) -> None:
        item = self.bookmark_list.currentItem()
        mark_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        mark = self.marks_store.get(mark_id)
        if mark is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete note or mark?",
            f"Delete the mark in “{mark.chapter_title}”?\n\nIts note and tags will also be removed.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.marks_store.remove(mark_id)
        self._populate_bookmarks()
        self.marks_dialog.refresh()

    def show_all_marks(self) -> None:
        """Show the searchable index even when the welcome/library page is active."""
        self.marks_store.load()
        self.marks_dialog.refresh()
        self.marks_dialog.show()
        self.marks_dialog.raise_()
        self.marks_dialog.activateWindow()

    def _open_global_mark(self, mark_id: str) -> None:
        self.marks_store.load()
        mark = self.marks_store.get(mark_id)
        if mark is None:
            QMessageBox.warning(self, "Mark not found", "That mark no longer exists.")
            return
        target = Path(mark.book_path).expanduser().resolve()
        if not target.is_file():
            QMessageBox.warning(
                self,
                "Book not found",
                f"The book for this mark is no longer at:\n{target}",
            )
            return
        current = self.book.path.resolve() if self.book else None
        if current != target:
            self.open_book(str(target))
        if not self.book or self.book.path.resolve() != target:
            return
        chapter_index = min(max(mark.chapter_index, 0), len(self.book.chapters) - 1)
        self._enter_reading_mode()
        self.show_chapter(chapter_index, mark.scroll_percent)

    def _migrate_legacy_bookmarks(self) -> None:
        """Move bookmarks from the old per-user settings file into the visible library file."""
        if not self.book:
            return
        state = self.store.book_state(self.book.key)
        legacy = state.get("bookmarks", [])
        if not isinstance(legacy, list) or not legacy:
            return
        self.marks_store.load()
        migrated = False
        for data in legacy:
            if not isinstance(data, dict):
                continue
            try:
                old = Bookmark.from_dict(data)
            except (TypeError, ValueError):
                continue
            if old.chapter_index >= len(self.book.chapters):
                continue
            if self.marks_store.has_position(
                str(self.book.path), old.chapter_index, old.scroll_percent
            ):
                continue
            mark = ReadingMark.create(
                book_path=str(self.book.path),
                book_title=self.book.metadata.title,
                book_author=self.book.metadata.author_line,
                chapter_index=old.chapter_index,
                chapter_title=old.chapter_title,
                scroll_percent=old.scroll_percent,
                overall_percent=(old.chapter_index + old.scroll_percent)
                / max(len(self.book.chapters), 1),
            )
            if old.created_at:
                mark.created_at = old.created_at
                mark.updated_at = old.created_at
            self.marks_store.marks.append(mark)
            migrated = True
        if migrated:
            self.marks_store.save()
        state["bookmarks"] = []
        self.store.save()

    def _handle_link(self, url: QUrl) -> None:
        if not self.book:
            return
        if url.scheme() in {"http", "https", "mailto"}:
            self._open_external_link(url)
            return
        index, fragment = self.book.chapter_index_for_url(url.toString())
        if index is None:
            return
        if index == self.chapter_index and fragment:
            fragment_json = json.dumps(fragment)
            self.web.page().runJavaScript(
                f"document.getElementById({fragment_json})?.scrollIntoView({{behavior:'smooth'}});"
            )
        else:
            self.show_chapter(index)
            if fragment:
                fragment_json = json.dumps(fragment)
                QTimer.singleShot(
                    150,
                    lambda: self.web.page().runJavaScript(
                        f"document.getElementById({fragment_json})?.scrollIntoView();"
                    ),
                )

    def _handle_new_window_request(self, request: Any) -> None:
        """Route Ctrl+click requests that Chromium classifies as a new tab."""
        if not control_link_activation_allowed(QApplication.keyboardModifiers()):
            return
        url = request.requestedUrl()
        if url.scheme() in {"http", "https", "mailto"}:
            self._open_external_link(url)
        else:
            # Internal EPUB links stay inside the reader, but remain just as
            # deliberate: this path is reachable only during Ctrl+click.
            QTimer.singleShot(0, lambda target=QUrl(url): self._handle_link(target))

    def _open_external_link(self, url: QUrl) -> None:
        """Open one deliberate external link once, even if Qt reports it twice."""
        text_url = url.toString()
        now = time.monotonic()
        if text_url == self._last_external_link and now - self._last_external_link_at < 0.8:
            return
        self._last_external_link = text_url
        self._last_external_link_at = now
        QDesktopServices.openUrl(url)

    def _poll_scroll(self) -> None:
        if not self.book or self.main_stack.currentIndex() != 1 or self._slider_is_dragging:
            return
        script = (
            "(() => { const max = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);"
            " return max ? window.scrollY / max : 0; })()"
        )
        self.web.page().runJavaScript(script, self._scroll_received)

    def _scroll_received(self, value: Any) -> None:
        if isinstance(value, (int, float)):
            self.scroll_percent = max(0.0, min(float(value), 1.0))
            self._update_overall_progress(self.scroll_percent)

    def _update_overall_progress(self, chapter_scroll: float) -> None:
        if not self.book:
            return
        overall = (self.chapter_index + chapter_scroll) / len(self.book.chapters)
        if not self._slider_is_dragging:
            self.progress_slider.setValue(round(chapter_scroll * 1000))
        self.percent_label.setText(f"{round(overall * 100)}%")

    def _slider_pressed(self) -> None:
        self._slider_is_dragging = True

    def _slider_released(self) -> None:
        self._slider_is_dragging = False
        percent = self.progress_slider.value() / 1000
        self.scroll_percent = percent
        self.web.page().runJavaScript(
            "(() => { const max = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);"
            f" window.scrollTo(0, max * {percent!r}); }})()"
        )
        self._update_overall_progress(percent)

    def _change_font_size(self, delta: int) -> None:
        if self.book and getattr(self.book, "document_type", "EPUB") == "PDF":
            return
        size = max(14, min(32, int(self.store.data.get("font_size", 20)) + delta))
        if size == self.store.data.get("font_size"):
            return
        self.store.data["font_size"] = size
        self._rerender_current()

    def _reset_font_size(self) -> None:
        if self.book and getattr(self.book, "document_type", "EPUB") == "PDF":
            return
        self.store.data["font_size"] = 20
        self._rerender_current()

    def _theme_changed(self, label: str) -> None:
        self.store.data["theme"] = THEME_NAMES.get(label, "dark")
        self._apply_app_theme()
        self._rerender_current()

    def show_definition_settings(self) -> None:
        dialog = DefinitionSettingsDialog(self, self.definition_fallbacks)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.definition_fallbacks = dialog.values()
        self.store.data["definition_fallbacks"] = dict(self.definition_fallbacks)
        self.store.save()

    def _rerender_current(self) -> None:
        if self.book:
            self.show_chapter(self.chapter_index, self.scroll_percent)
        self.store.save()

    def _toggle_sidebar(self) -> None:
        visible = not self.sidebar.isVisible()
        self.sidebar.setVisible(visible)
        self.store.data["sidebar_visible"] = visible

    def _remember_progress(self) -> None:
        if not self.book:
            return
        state = self.store.book_state(self.book.key)
        state["chapter"] = self.chapter_index
        state["scroll"] = round(self.scroll_percent, 6)

    def save_state(self) -> None:
        self._remember_progress()
        self.store.save()

    def _header_minimum_width(self) -> int:
        """Return the real minimum width of the explicitly visible header items."""

        margins = self.header_layout.contentsMargins()
        required = margins.left() + margins.right()
        visible_items = 0
        for index in range(self.header_layout.count()):
            widget = self.header_layout.itemAt(index).widget()
            if widget is None or widget.isHidden():
                continue
            visible_items += 1
            if widget is self.reader_identity:
                # The two-line identity block is deliberately allowed to
                # collapse; reserve a useful title/path preview only while
                # deciding which lower-priority actions to hide.
                required += 160 if not self.reader_search_cluster.isHidden() else 120
                continue
            hint = widget.minimumSizeHint().width()
            required += max(0, widget.minimumWidth(), hint)
        if visible_items > 1:
            required += self.header_layout.spacing() * (visible_items - 1)
        return required

    def _update_header_responsiveness(self) -> None:
        """Fit the header without ever allowing one control to cover another."""

        if not hasattr(self, "_optional_header_widgets"):
            return
        self.brand.show()
        for widget in self._optional_header_widgets:
            widget.show()

        available = max(0, self.width())
        self.header_layout.invalidate()
        for widget in self._optional_header_widgets:
            if self._header_minimum_width() <= available:
                break
            widget.hide()
            self.header_layout.invalidate()

        # The brand is decorative, while every remaining item is a direct
        # reading control.  On the smallest supported window it is the final
        # safe release valve after all optional actions have collapsed.
        if self._header_minimum_width() > available:
            self.brand.hide()
            self.header_layout.invalidate()
        self.header_layout.activate()

    @staticmethod
    def _is_inside(widget: QWidget, container: QWidget) -> bool:
        current: QWidget | None = widget
        while current is not None:
            if current is container:
                return True
            current = current.parentWidget()
        return False

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "reader_search_edit"):
            return
        if self.width() >= 1500:
            edit_width = 210
        elif self.width() >= 1280:
            edit_width = 180
        elif self.width() >= 1100:
            edit_width = 130
        else:
            edit_width = 90
        self.reader_search_edit.setFixedWidth(edit_width)
        self.reader_search_cluster.setFixedWidth(edit_width + 65)
        self._update_header_responsiveness()

    def eventFilter(self, watched: Any, event: Any) -> bool:
        """Route precision-trackpad scrolls received by non-scrollable chrome.

        Qt's list views and WebEngine consume their own wheel events. This
        catches gestures over labels, toolbars, tab headers, and other passive
        surfaces so two-finger scrolling never appears to stop working merely
        because the pointer crossed a widget boundary.
        """
        if not isinstance(watched, QWidget):
            return super().eventFilter(watched, event)
        if watched is not self and not self.isAncestorOf(watched):
            return super().eventFilter(watched, event)
        if watched is self.reader_search_edit and event.type() in {
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        }:
            self.reader_search_cluster.setProperty(
                "searchFocus", event.type() == QEvent.Type.FocusIn
            )
            self.reader_search_cluster.style().unpolish(self.reader_search_cluster)
            self.reader_search_cluster.style().polish(self.reader_search_cluster)
        if (
            event.type() in {QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride}
            and self._is_inside(watched, self.web)
            and event.key() in {Qt.Key.Key_Tab, Qt.Key.Key_Backtab}
            and not event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            # Chromium must never advance focus to a book anchor. Link tags
            # are also tabindex=-1 in the document; this is the native-event
            # backstop for malformed content and renderer edge cases.
            event.accept()
            return True
        if (
            self._speed_target_active
            and event.type() in {QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride}
            and event.key() == Qt.Key.Key_Escape
        ):
            event.accept()
            if event.type() == QEvent.Type.KeyPress:
                self._cancel_speed_start_target()
            return True
        if self._speed_target_active and self._is_inside(watched, self.web):
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._selection_press_position = None
                self._selection_dragged = False
                QTimer.singleShot(25, self._take_speed_target_pick)
            if event.type() in {
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
                QEvent.Type.MouseMove,
            }:
                return super().eventFilter(watched, event)
        if self._speed_return_highlight_visible or self._pending_speed_return_highlight is not None:
            if (
                event.type() in {QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride}
                and event.key() == Qt.Key.Key_Escape
            ):
                event.accept()
                if event.type() == QEvent.Type.KeyPress:
                    self._dismiss_speed_return_highlight()
                return True
            if event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.Wheel}:
                self._dismiss_speed_return_highlight()
        if event.type() == QEvent.Type.MouseButtonPress:
            if (
                event.button() == Qt.MouseButton.LeftButton
                and self._is_inside(watched, self.web)
            ):
                self._selection_press_position = event.globalPosition().toPoint()
                self._selection_dragged = False
            if self.definition_card.isVisible() and not self._is_inside(watched, self.definition_card):
                self.definition_card.dismiss()
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.MouseMove
            and self._selection_press_position is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and self._is_inside(watched, self.web)
        ):
            distance = (
                event.globalPosition().toPoint() - self._selection_press_position
            ).manhattanLength()
            if distance >= 6 and not self._selection_dragged:
                self._selection_dragged = True
                self._selection_capture_id += 1
                self._selection_candidate = ""
                self.selection_prompt_timer.stop()
                self.selection_prompt.dismiss()
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self._is_inside(watched, self.web)
        ):
            was_dragged = self._selection_dragged
            self._selection_press_position = None
            self._selection_dragged = False
            if was_dragged:
                self._selection_capture_id += 1
                capture_id = self._selection_capture_id
                global_position = event.globalPosition().toPoint()
                QTimer.singleShot(
                    65,
                    lambda point=global_position, serial=capture_id: self._capture_drag_selection(
                        point, serial
                    ),
                )
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonDblClick and self._is_inside(watched, self.web):
            if self._has_multiword_selection_candidate():
                event.accept()
                return True
            self._clear_selection_candidate()
            global_position = event.globalPosition().toPoint()
            QTimer.singleShot(65, lambda point=global_position: self._request_definition_at(point))
            return super().eventFilter(watched, event)
        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(watched, event)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return super().eventFilter(watched, event)

        # Native precision scrolling is best when the gesture is already over a
        # scroll view or over the Chromium surface.
        if self._is_inside(watched, self.web):
            return super().eventFilter(watched, event)
        current: QWidget | None = watched
        while current is not None:
            if isinstance(current, QAbstractScrollArea):
                return super().eventFilter(watched, event)
            current = current.parentWidget()

        pixel = event.pixelDelta()
        angle = event.angleDelta()
        delta_x = pixel.x() if not pixel.isNull() else angle.x() / 120 * 72
        delta_y = pixel.y() if not pixel.isNull() else angle.y() / 120 * 72
        if not delta_x and not delta_y:
            return super().eventFilter(watched, event)

        if self.main_stack.currentIndex() == 0:
            bar = self.welcome.view.verticalScrollBar()
            bar.setValue(round(bar.value() - delta_y))
        elif self._is_inside(watched, self.sidebar):
            views = (self.toc_tree, self.search_results, self.bookmark_list)
            view = views[self.tabs.currentIndex()]
            bar = view.verticalScrollBar()
            bar.setValue(round(bar.value() - delta_y))
        elif self.book:
            self.web.page().runJavaScript(
                f"window.scrollBy({-delta_x!r}, {-delta_y!r});"
            )
        event.accept()
        return True

    def dragEnterEvent(self, event: Any) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() and is_supported_book(url.toLocalFile()) for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event: Any) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and is_supported_book(url.toLocalFile()):
                self.open_book(url.toLocalFile())
                event.acceptProposedAction()
                break

    def closeEvent(self, event: Any) -> None:
        if self._speed_target_active:
            self._cancel_speed_start_target()
        # A sweep owns a fleet of real OS processes.  Leaving them running after
        # the window is gone would strand a dozen high-priority orphans, so the
        # scanner is told to stop and given a moment to shut its fleet down.
        if self._sweep_timer is not None:
            self._sweep_timer.stop()
            self._sweep_timer = None
        if self._scanner is not None and self._scanner.is_running():
            self._scanner.cancel()
            self._scanner.wait(15.0)
        self.save_state()
        self._cancel_dictionary_lookup()
        self._dictionary_executor.shutdown(wait=False, cancel_futures=True)
        if self.book:
            self.book.close()
        super().closeEvent(event)

    def _apply_app_theme(self) -> None:
        theme = str(self.store.data.get("theme", "dark"))
        palettes = {
            "dark": {
                "bg": "#0b0e14", "panel": "#111620", "panel2": "#171d29", "fg": "#ecedea",
                "muted": "#929bad", "line": "#252d3b", "accent": "#63d1ad", "accent2": "#112d28",
                "hover": "#202837", "reading": "#11141c",
            },
            "light": {
                "bg": "#e9e5dc", "panel": "#f7f4ed", "panel2": "#ffffff", "fg": "#22252a",
                "muted": "#6b7280", "line": "#d7d2c8", "accent": "#137c69", "accent2": "#d9eee8",
                "hover": "#ebe7df", "reading": "#f6f2e9",
            },
            "sepia": {
                "bg": "#d8ccb3", "panel": "#eadfc8", "panel2": "#f4ead4", "fg": "#3d3328",
                "muted": "#766650", "line": "#c9b997", "accent": "#875b2d", "accent2": "#dfc9a5",
                "hover": "#e3d5ba", "reading": "#efe4cc",
            },
        }
        c = palettes.get(theme, palettes["dark"])
        # Kept so the sweep monitor and the configuration window can be opened
        # in the reader's palette rather than always in the dark one.
        self.theme_colors = dict(c)
        self.welcome.apply_palette(c)
        if self._monitor is not None:
            self._monitor.apply_palette(c)
        self.setStyleSheet(f"""
            QMainWindow, QDialog, #appShell {{ background: {c['bg']}; color: {c['fg']}; }}
            QWidget {{ font-family: 'Segoe UI'; font-size: 13px; }}
            #header {{ background: {c['panel']}; border-bottom: 1px solid {c['line']}; }}
            #brand {{ color: {c['accent']}; font-size: 15px; font-weight: 800; letter-spacing: 3px; }}
            #libraryButton {{ color: {c['accent']}; background: {c['accent2']}; border: 1px solid {c['accent']}; border-radius: 16px; padding: 6px 13px; font-size: 10px; font-weight: 750; letter-spacing: 1px; }}
            #libraryButton:hover {{ color: #09130f; background: {c['accent']}; }}
            #libraryButton:pressed {{ padding-left: 11px; padding-right: 15px; }}
            #chapterHeading {{ color: {c['muted']}; font-size: 13px; padding-left: 5px; }}
            #sourcePathLabel {{ color: {c['accent']}; font-size: 9px; font-weight: 650; letter-spacing: 0.35px; padding-left: 5px; }}
            #sourcePathCopy {{ color: {c['accent']}; background: transparent; border: none; padding: 0; font-size: 10px; font-weight: 800; }}
            #sourcePathCopy:hover {{ color: #09130f; background: {c['accent']}; border-radius: 4px; }}
            #headerDivider {{ color: {c['line']}; margin: 8px 5px; }}
            QPushButton {{ color: {c['fg']}; background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 7px 11px; }}
            QPushButton:hover {{ background: {c['hover']}; }}
            QPushButton:disabled {{ color: {c['muted']}; background: transparent; }}
            #iconButton {{ font-size: 19px; min-width: 30px; padding: 6px; }}
            #compactButton {{ background: {c['panel2']}; border-color: {c['line']}; min-width: 34px; }}
            #toolButton, #subtleButton {{ background: {c['panel2']}; border-color: {c['line']}; }}
            #speedReaderButton {{ color: {c['accent']}; background: {c['accent2']}; border: 1px solid {c['accent']}; font-weight: 700; }}
            #speedReaderButton:hover {{ color: #09130f; background: {c['accent']}; }}
            #primaryButton, #primarySmallButton {{ background: {c['accent']}; color: #09130f; font-weight: 700; border: none; }}
            #primaryButton:hover, #primarySmallButton:hover {{ background: {c['accent']}; }}
            QComboBox {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 7px; padding: 7px 28px 7px 10px; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{ background: {c['panel2']}; color: {c['fg']}; selection-background-color: {c['accent2']}; }}
            #welcomePage {{ background: {c['bg']}; }}
            #welcomeScroll {{ background: {c['bg']}; border: none; }}
            #welcomeScroll > QWidget > QWidget {{ background: {c['bg']}; }}
            #heroMark {{ color: #09130f; background: {c['accent']}; border-radius: 18px; font: 800 36px Georgia; }}
            #heroHeading {{ color: {c['fg']}; font: 650 32px 'Segoe UI'; }}
            #heroSubheading {{ color: {c['muted']}; font-size: 15px; line-height: 1.4; }}
            #eyebrow {{ color: {c['accent']}; font-size: 11px; font-weight: 750; letter-spacing: 2px; padding-top: 7px; }}
            #shelfSearch {{ background: {c['panel']}; border-color: {c['line']}; padding: 11px 13px; font-size: 14px; }}
            #shelfSearch:focus {{ border-color: {c['accent']}; }}
            #shelfList {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 13px; padding: 8px; outline: none; }}
            #shelfList::item {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 12px 14px; margin: 2px; }}
            #shelfList::item:selected {{ border-color: {c['accent']}; background: {c['accent2']}; }}
            #sidebar {{ background: {c['panel']}; border-right: 1px solid {c['line']}; }}
            #cover {{ background: {c['panel2']}; color: {c['accent']}; border: 1px solid {c['line']}; border-radius: 4px; font-weight: 700; }}
            #bookTitle {{ color: {c['fg']}; font-size: 15px; font-weight: 650; }}
            #bookAuthor {{ color: {c['muted']}; font-size: 12px; }}
            QTabWidget::pane {{ border: none; border-top: 1px solid {c['line']}; top: -1px; }}
            QTabBar::tab {{ color: {c['muted']}; background: transparent; padding: 12px 9px 10px; border-bottom: 2px solid transparent; }}
            QTabBar::tab:selected {{ color: {c['fg']}; border-bottom-color: {c['accent']}; }}
            QTreeWidget, QListWidget {{ color: {c['fg']}; background: transparent; border: none; outline: none; }}
            QTreeWidget::item, QListWidget::item {{ border-radius: 6px; padding: 6px; }}
            QTreeWidget::item:selected, QListWidget::item:selected {{ color: {c['fg']}; background: {c['accent2']}; }}
            QTreeWidget::item:hover, QListWidget::item:hover {{ background: {c['hover']}; }}
            QLineEdit {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 7px; padding: 8px 9px; selection-background-color: {c['accent']}; }}
            QLineEdit:focus {{ border-color: {c['accent']}; }}
            #readerSearchCluster {{ background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 9px; }}
            #readerSearchCluster[searchFocus="true"], #readerSearchCluster[searchState="found"] {{ border-color: {c['accent']}; }}
            #readerSearchCluster[searchState="miss"] {{ border-color: #d87373; }}
            #readerSearchEdit {{ background: transparent; border: none; border-radius: 0; padding: 7px 9px; }}
            #readerSearchEdit:focus {{ border: none; }}
            #readerSearchButton {{ color: {c['fg']}; background: transparent; border: none; border-left: 1px solid {c['line']}; border-radius: 0; padding: 6px 4px; font-size: 14px; }}
            #readerSearchButton:hover {{ color: #09130f; background: {c['accent']}; border-color: {c['accent']}; }}
            #readerSearchOptions {{ color: {c['muted']}; background: transparent; border: none; border-left: 1px solid {c['line']}; border-radius: 0; border-top-right-radius: 8px; border-bottom-right-radius: 8px; padding: 6px 2px; }}
            #readerSearchOptions:hover, #readerSearchOptions::menu-button:hover {{ color: #09130f; background: {c['accent']}; }}
            #readerSearchOptions::menu-indicator {{ image: none; width: 0; }}
            #readerSearchMenu {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; padding: 7px; }}
            #readerSearchMenu::item {{ border-radius: 6px; padding: 7px 28px 7px 10px; }}
            #readerSearchMenu::item:selected {{ background: {c['hover']}; }}
            #readerSearchMenu::item:checked {{ color: {c['accent']}; background: {c['accent2']}; }}
            #readerSearchMenu::separator {{ background: {c['line']}; height: 1px; margin: 6px 4px; }}
            QTextEdit {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 7px; padding: 8px 9px; selection-background-color: {c['accent']}; }}
            QTextEdit:focus {{ border-color: {c['accent']}; }}
            #dialogHeading {{ color: {c['fg']}; font-size: 23px; font-weight: 700; }}
            #dialogIntro, #settingsStatus {{ color: {c['muted']}; font-size: 12px; }}
            QCheckBox {{ color: {c['fg']}; spacing: 9px; padding: 4px 1px; }}
            QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {c['line']}; border-radius: 5px; background: {c['panel2']}; }}
            QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
            #markContext, #marksSummary {{ color: {c['muted']}; font-size: 12px; }}
            #markQuote {{ color: {c['fg']}; background: {c['accent2']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 10px; }}
            #marksList {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 10px; padding: 7px; }}
            #marksList::item {{ background: {c['panel2']}; border: 1px solid {c['line']}; margin: 2px; padding: 9px; }}
            #marksList::item:selected {{ background: {c['accent2']}; border-color: {c['accent']}; }}
            #bookView {{ background: {c['reading']}; }}
            #footer {{ background: {c['panel']}; border-top: 1px solid {c['line']}; }}
            #footerLabel {{ color: {c['muted']}; font-size: 12px; }}
            #selectionLookupPrompt {{ background: {c['accent2']}; border: 1px solid {c['accent']}; border-radius: 14px; }}
            #selectionLookupButton {{ color: {c['fg']}; background: transparent; border: none; border-radius: 11px; padding: 10px 15px; font-size: 11px; font-weight: 750; text-align: left; }}
            #selectionLookupButton:hover {{ color: #09130f; background: {c['accent']}; }}
            #selectionLookupButton:pressed {{ padding-top: 11px; padding-bottom: 9px; }}
            #definitionCard {{ background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 13px; }}
            #definitionEyebrow {{ color: {c['accent']}; font-size: 10px; font-weight: 750; letter-spacing: 1.6px; }}
            #definitionClose {{ color: {c['muted']}; border: none; background: transparent; font-size: 20px; padding: 0; }}
            #definitionClose:hover {{ color: {c['fg']}; background: {c['hover']}; }}
            #definitionWord {{ color: {c['fg']}; font-size: 24px; font-weight: 700; }}
            #definitionMeta {{ color: {c['accent']}; font-size: 12px; font-style: italic; }}
            #definitionSpinner {{ color: {c['accent']}; font-size: 17px; font-weight: 700; }}
            #definitionStatus {{ color: {c['muted']}; font-size: 11px; }}
            #definitionProgress {{ background: {c['line']}; border: none; border-radius: 2px; }}
            #definitionProgress::chunk {{ background: {c['accent']}; border-radius: 2px; }}
            #definitionScroll, #definitionContent {{ background: transparent; border: none; }}
            #definitionBlock {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 9px; }}
            #definitionBlockSource {{ color: {c['accent']}; font-size: 9px; font-weight: 750; letter-spacing: 0.7px; }}
            #definitionEmpty {{ color: {c['muted']}; background: {c['panel']}; border: 1px dashed {c['line']}; border-radius: 9px; padding: 15px; font-size: 12px; }}
            #definitionText {{ color: {c['fg']}; font-size: 14px; line-height: 1.35; }}
            #definitionExample {{ color: {c['muted']}; font-size: 12px; font-style: italic; }}
            #definitionSynonyms {{ color: {c['fg']}; background: {c['accent2']}; border-radius: 6px; padding: 8px; font-size: 12px; }}
            #definitionRetry {{ color: {c['accent']}; background: {c['accent2']}; border: 1px solid {c['accent']}; padding: 6px 10px; font-size: 11px; font-weight: 650; }}
            #definitionRetry:hover {{ color: #09130f; background: {c['accent']}; }}
            #definitionSource {{ color: {c['muted']}; font-size: 9px; padding-top: 3px; }}
            QSlider::groove:horizontal {{ height: 4px; background: {c['line']}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {c['fg']}; border: 2px solid {c['accent']}; width: 13px; height: 13px; margin: -6px 0; border-radius: 7px; }}
            QSplitter::handle {{ background: {c['line']}; width: 1px; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: {c['line']}; min-height: 28px; border-radius: 5px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QToolTip {{ color: {c['fg']}; background: {c['panel2']}; border: 1px solid {c['line']}; padding: 5px; }}
        """)
        background = QColor(c["reading"])
        self.web.page().setBackgroundColor(background)
