from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from lumen_reader.ui import (
    RSVP_RETURN_HIGHLIGHT_STOP_SCRIPT,
    RSVP_TARGETING_SCRIPT,
    RSVP_TARGET_STOP_SCRIPT,
    RSVP_TARGET_TAKE_PICK_SCRIPT,
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

    assert payload == {"word": "beta.", "wordIndex": 1}
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
