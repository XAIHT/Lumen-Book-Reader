from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from lumen_reader import ui as ui_module
from lumen_reader.library_index import LibraryIndex
from lumen_reader.marks import MarksStore
from lumen_reader.speed_reader import (
    SpeedChapter,
    SpeedReaderSettings,
    SpeedReadingDocument,
)
from lumen_reader.storage import ReaderStore
from lumen_reader.ui import (
    RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT,
    RSVP_TARGETING_SCRIPT,
    RSVP_TARGET_STOP_SCRIPT,
    RSVP_TARGET_TAKE_PICK_SCRIPT,
    ReaderWindow,
    resolve_rsvp_target_word_index,
    rsvp_return_highlight_script,
)


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _run_javascript(page: QWebEnginePage, script: str) -> object:
    loop = QEventLoop()
    result: list[object] = []
    page.runJavaScript(script, lambda value: (result.append(value), loop.quit()))
    QTimer.singleShot(5_000, loop.quit)
    loop.exec()
    return result[0] if result else None


def _load_html(page: QWebEnginePage, html: str) -> None:
    loop = QEventLoop()
    page.loadFinished.connect(lambda _ok: loop.quit())
    page.setHtml(html, QUrl("about:blank"))
    QTimer.singleShot(5_000, loop.quit)
    loop.exec()


def _wait(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def _wait_until(predicate, timeout_ms: int = 5_000) -> bool:
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)

    def inspect() -> None:
        if predicate():
            loop.quit()

    timer.timeout.connect(inspect)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return bool(predicate())


def test_cursor_targeting_returns_the_exact_visible_word_index() -> None:
    app = _application()
    view = QWebEngineView()
    view.resize(800, 600)
    view.show()
    app.processEvents()
    page = view.page()
    _load_html(
        page,
        """<!doctype html><html><head><style>
        body { margin: 60px; font: 24px serif; }
        </style></head><body>
        <div class="lumen-section-label">Invisible reader label</div>
        <p>Alpha <em id="target">beta.</em> Gamma delta</p>
        </body></html>""",
    )

    assert _run_javascript(page, RSVP_TARGETING_SCRIPT) is True
    _run_javascript(
        page,
        """(() => {
          const node = document.getElementById('target').firstChild;
          const range = document.createRange();
          range.selectNodeContents(node);
          const rect = range.getBoundingClientRect();
          const event = new PointerEvent('pointerdown', {
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2,
            button: 0,
            bubbles: true
          });
          document.getElementById('target').dispatchEvent(event);
          return true;
        })()""",
    )
    payload = json.loads(str(_run_javascript(page, RSVP_TARGET_TAKE_PICK_SCRIPT)))

    assert payload == {
        "word": "beta.",
        "wordIndex": 1,
        "contextBefore": ["Alpha"],
        "contextAfter": ["Gamma", "delta"],
    }
    assert _run_javascript(page, RSVP_TARGET_STOP_SCRIPT) is True
    assert _run_javascript(
        page, "document.documentElement.classList.contains('lumen-rsvp-targeting')"
    ) is False

    returned = json.loads(
        str(_run_javascript(page, rsvp_return_highlight_script(1, 2)))
    )
    assert returned == {"found": True, "wordIndex": 1, "wordCount": 2}
    _wait(100)
    red_marker = json.loads(
        str(
            _run_javascript(
                page,
                """JSON.stringify({
                  segments: document.querySelectorAll('.lumen-rsvp-return-segment').length,
                  label: document.getElementById('lumen-rsvp-return-tag')?.textContent || ''
                })""",
            )
        )
    )
    assert red_marker == {"segments": 1, "label": "LAST PHRASE READ"}
    assert _run_javascript(page, RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT) is True
    assert _run_javascript(
        page, "document.getElementById('lumen-rsvp-return-highlight') === null"
    ) is True

    view.close()
    view.deleteLater()
    app.processEvents()


def test_a_chromium_pick_launches_rsvp_without_a_native_mouse_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """WebEngine may consume Qt's release event; the recorded pick must still launch."""
    app = _application()
    monkeypatch.setattr(ui_module, "lookup_offline_wordnet_entries", lambda *_args: [])
    index = LibraryIndex(tmp_path / "library.db")
    window = ReaderWindow(
        ReaderStore(tmp_path / "reader.json"),
        marks_store=MarksStore(tmp_path / "marks.json"),
        library_root=tmp_path,
        library_index=index,
    )
    window.book = SimpleNamespace(chapters=[SimpleNamespace(title="Chapter")])
    window.chapter_index = 0
    window.main_stack.setCurrentIndex(1)
    window.resize(1000, 700)
    window.show()
    app.processEvents()
    _load_html(
        window.web.page(),
        """<!doctype html><html><head><style>
        body { margin: 60px; font: 24px serif; }
        </style></head><body>
        <div>Visible browser-only prefix shifts every following DOM word index</div>
        <p>Alpha <em id="target">beta.</em> Gamma delta</p>
        </body></html>""",
    )

    document = SpeedReadingDocument(
        [SpeedChapter("Chapter", ["Alpha", "beta.", "Gamma", "delta"])]
    )
    settings = SpeedReaderSettings()
    launched: list[tuple[int, int]] = []
    window._launch_speed_reader = (
        lambda _document, _settings, chapter, word: launched.append((chapter, word))
    )
    window._begin_speed_start_target(document, settings)

    _run_javascript(
        window.web.page(),
        """(() => {
          const node = document.getElementById('target').firstChild;
          const range = document.createRange();
          range.selectNodeContents(node);
          const rect = range.getBoundingClientRect();
          document.getElementById('target').dispatchEvent(new PointerEvent('pointerdown', {
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2,
            button: 0,
            bubbles: true
          }));
          return true;
        })()""",
    )

    assert _wait_until(lambda: bool(launched))
    assert launched == [(0, 1)]
    assert window._speed_target_active is False
    assert window.speed_target_poll_timer.isActive() is False

    window.book = None
    window.close()
    window.deleteLater()
    app.processEvents()
    index.close()


def test_shifted_dom_context_selects_the_intended_duplicate_word() -> None:
    words = [
        "Te",
        "preocupa",
        "el",
        "qué",
        "dirán.",
        "Te",
        "cuesta",
        "decir",
        "que",
        "no.",
        "Cuánto",
        "cuesta",
        "molestar?",
    ]

    assert resolve_rsvp_target_word_index(
        words,
        {
            "word": "cuesta",
            "wordIndex": 16,
            "contextBefore": ["el", "qué", "dirán.", "Te"],
            "contextAfter": ["decir", "que", "no."],
        },
    ) == 6


def test_ambiguous_shifted_dom_context_does_not_guess() -> None:
    words = ["same", "target", "same", "same", "target", "same"]

    assert resolve_rsvp_target_word_index(
        words,
        {
            "word": "target",
            "wordIndex": 20,
            "contextBefore": ["same"],
            "contextAfter": ["same"],
        },
    ) is None
