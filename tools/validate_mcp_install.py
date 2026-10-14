r"""Visible MCP handoff validation. No offscreen mode; Shoter is mandatory.

Example (PowerShell):
  .venv-release\Scripts\python.exe tools\validate_mcp_install.py --build
    --shoter-launcher C:\path\to\harness\shoter_shot.py
    --photo-python 'C:\Program Files\Python312\python.exe'

Without --build supply --package <release-folder>\pkg.zip. Never registers the
test installation with Windows or changes a real reader/client configuration.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def capture(name: str) -> None:
    result = subprocess.run(
        [os.environ["LUMEN_PHOTO_PYTHON"], str(Path(__file__).resolve()), "--photo", name],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
    )
    if result.returncode:
        raise RuntimeError("SHOTER_EVIDENCE_FAILED: " + result.stdout + result.stderr)
    print(result.stdout.strip(), flush=True)


_test_results = {}


def pytest_runtest_logreport(report):
    if os.environ.get("LUMEN_VISIBLE_TESTS") != "1":
        return
    if report.when == "call" or report.failed:
        _test_results[report.nodeid] = report
    if report.when == "teardown" and report.nodeid in _test_results:
        # Capture only after fixture teardown restores subprocess/Path mocks;
        # injected failure tests must not break the evidence helper itself.
        report = _test_results.pop(report.nodeid)
        import hashlib
        name = hashlib.sha256(report.nodeid.encode()).hexdigest()[:12]
        print(f"\nVisible result: {report.nodeid} — {report.outcome}", flush=True)
        time.sleep(0.25)  # let the live window paint the result before the photo
        capture(f"test-{name}-{report.outcome}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--integration-only", action="store_true",
                        help="Rerun only native installation and live MCP checks against --package")
    parser.add_argument("--shoter-launcher", type=Path)
    parser.add_argument("--photo-python", default=sys.executable)
    parser.add_argument("--photo")
    options = parser.parse_args()
    if options.photo:
        launcher = Path(os.environ["LUMEN_SHOTER_LAUNCHER"])
        spec = importlib.util.spec_from_file_location("lumen_shoter_launcher", launcher)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evidence = Path(os.environ["LUMEN_MCP_EVIDENCE"])
        runtime = tempfile.mkdtemp(prefix="shoter-", dir=evidence)
        image = module.take_shot(str(evidence), options.photo + ".png", runtime_base=runtime, timeout=30)
        if not image:
            raise SystemExit("SHOTER_EVIDENCE_FAILED: no photo produced; no fallback used")
        print("Shoter:", image)
        return 0

    if os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}:
        parser.error("Visible tests require the native desktop")
    if not options.shoter_launcher or not options.shoter_launcher.is_file():
        parser.error("Named prerequisite: --shoter-launcher must point to shoter_shot.py")
    if not Path(options.photo_python).is_file():
        parser.error("Named prerequisite: --photo-python interpreter does not exist")
    if not options.build and (not options.package or not options.package.is_file()):
        parser.error("Supply --build or an existing --package pkg.zip")
    if options.build and options.integration_only:
        parser.error("--integration-only requires an already-built --package")

    from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

    evidence = ROOT / ".artifacts" / "mcp-install" / time.strftime("%Y%m%d-%H%M%S")
    evidence.mkdir(parents=True)
    package = ROOT / "pkg.zip" if options.build else options.package.resolve()
    os.environ.update({
        "LUMEN_VISIBLE_TESTS": "1", "LUMEN_TEST_PACKAGE": str(package),
        "LUMEN_MCP_EVIDENCE": str(evidence),
        "LUMEN_PHOTO_PYTHON": str(Path(options.photo_python).resolve()),
        "LUMEN_SHOTER_LAUNCHER": str(options.shoter_launcher.resolve()),
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
    })
    tests = ["-m", "pytest", "-v", "-s", "--tb=short", "-p", "tools.validate_mcp_install"]
    stages = [("Exporter + installer regression tests", [*tests,
               "--junitxml=" + str(evidence / "regressions.xml"),
               "tests/test_mcp_config_export.py", "tests/test_installer_mcp_config.py",
               "tests/test_release_scheme.py"])]
    if options.integration_only:
        stages.clear()
    if options.build:
        stages.append(("Build actual application + MCP payload", ["build.py"]))
    stages.append(("Visible installation + live frozen MCP", [*tests,
                   "--junitxml=" + str(evidence / "integration.xml"),
                   "tests/test_installed_mcp_handoff.py"]))
    if options.build:
        stages.extend([("Build uninstaller", ["build_uninstaller.py"]),
                       ("Build installer + assemble release", ["build_installer.py"])])

    app = QApplication([])
    window = QWidget()
    window.setWindowTitle("Lumen MCP installation — LIVE validation")
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    layout = QVBoxLayout(window)
    heading = QLabel("Visible tests — temporary installation, real frozen MCP")
    output = QPlainTextEdit()
    output.setReadOnly(True)
    layout.addWidget(heading)
    layout.addWidget(output)
    window.setStyleSheet("QWidget{background:#10151f;color:#edf0ed;} QLabel{color:#63d1ad;font-size:18px;} QPlainTextEdit{font:13px Consolas;}")
    window.resize(1160, 780)
    window.show()
    window.raise_()
    window.activateWindow()
    process = QProcess(window)
    process.setWorkingDirectory(str(ROOT))
    process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    log = (evidence / "validation.log").open("w", encoding="utf-8", buffering=1)
    state = {"index": 0, "code": 1}

    def drain():
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        log.write(text)
        output.moveCursor(output.textCursor().MoveOperation.End)
        output.insertPlainText(text)

    def stop(code):
        state["code"] = code
        heading.setText("PASS — all stages completed" if not code else "FAILED — see the exact diagnostics below")
        (evidence / "exit-code.txt").write_text(str(code), encoding="utf-8")
        log.close()
        QTimer.singleShot(8000, app.quit)

    def advance():
        if state["index"] == len(stages):
            stop(0)
            return
        title, arguments = stages[state["index"]]
        heading.setText(f'{state["index"] + 1}/{len(stages)} — {title}')
        text = "\n" + title + "\n$ " + subprocess.list2cmdline([sys.executable, *arguments]) + "\n"
        output.appendPlainText(text)
        log.write(text)
        process.start(sys.executable, arguments)

    def finished(code, status):
        drain()
        if code or status != QProcess.ExitStatus.NormalExit:
            stop(code or 1)
        else:
            state["index"] += 1
            QTimer.singleShot(400, advance)

    process.readyReadStandardOutput.connect(drain)
    process.finished.connect(finished)
    process.errorOccurred.connect(lambda _: stop(5) if process.state() == QProcess.ProcessState.NotRunning else None)
    QTimer.singleShot(700, advance)
    app.exec()
    return state["code"]


if __name__ == "__main__":
    raise SystemExit(main())
