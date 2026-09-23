r"""Run date regression tests with a visible native Qt progress window.

Usage: .venv-release\Scripts\python.exe tools\validate_book_dates.py
Never selects offscreen/minimal rendering. UI tests show their actual windows.
The final test transcript is kept in .artifacts/book-dates/validation.log.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QVBoxLayout, QWidget


def main() -> int:
    if os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}:
        raise SystemExit("Visible tests require a native desktop; remove QT_QPA_PLATFORM=offscreen/minimal.")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = QWidget()
    window.setWindowTitle("Lumen Book Reader — LIVE date-feature validation")
    layout = QVBoxLayout(window)
    heading = QLabel("Running real sweep, migration, MCP and visible shelf tests…")
    log = QPlainTextEdit()
    log.setReadOnly(True)
    layout.addWidget(heading)
    layout.addWidget(log)
    window.setStyleSheet("QWidget {background:#10151f;color:#ecedea;} QLabel {color:#63d1ad;font-size:18px;padding:10px;} QPlainTextEdit {font-family:Consolas;font-size:12px;}")
    window.resize(1050, 700)
    window.show()
    process = QProcess(window)
    environment = QProcessEnvironment.systemEnvironment()
    environment.insert("PYTHONUNBUFFERED", "1")
    environment.insert("PYTHONIOENCODING", "utf-8")
    environment.insert("LUMEN_VISIBLE_TESTS", "1")
    process.setProcessEnvironment(environment)
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    root = Path(__file__).resolve().parents[1]
    process.setWorkingDirectory(str(root))
    transcript = []
    outcome = [1]

    def output():
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        transcript.append(text)
        log.moveCursor(log.textCursor().MoveOperation.End)
        log.insertPlainText(text)
        print(text, end="", flush=True)

    def finished(code, _status):
        output()
        outcome[0] = code
        heading.setText("PASS — visible validation finished" if code == 0 else "FAILED — see the exact failures below")
        target = root / ".artifacts" / "book-dates"
        target.mkdir(parents=True, exist_ok=True)
        (target / "validation.log").write_text("".join(transcript), encoding="utf-8")
        window.grab().save(str(target / "validation-window.png"))
        QTimer.singleShot(4000, app.quit)

    process.readyReadStandardOutput.connect(output)
    process.finished.connect(finished)
    targets = sys.argv[1:] or [
        "tests/test_book_dates.py", "tests/test_library_index.py", "tests/test_turbo_scan.py",
        "tests/test_shelf_ui.py", "tests/test_retrieval_service.py", "tests/test_mcp_server.py",
        "tests/test_mcp_stdio.py", "tests/test_fts_rowid_map.py", "tests/test_safety_and_storage.py",
        "tests/test_accel.py", "tests/test_date_backends.py",
    ]
    process.start(sys.executable, ["-m", "pytest", "-v", "--tb=short", *targets])
    app.exec()
    if process.state() != QProcess.ProcessState.NotRunning:
        process.terminate()
        process.waitForFinished(2000)
    return outcome[0]


if __name__ == "__main__":
    raise SystemExit(main())
