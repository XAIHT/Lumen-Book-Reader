"""The release scheme's mirror is enforced here, not merely intended.

``install.py`` writes registry keys, shortcuts and file associations;
``uninstall.py`` and the two PowerShell scripts remove them. That symmetry is
the entire design, and it is exactly the kind of thing that rots silently: add
a format to the installer, forget the unregistrar, and every user who ever
ticked it keeps a dead "Open with" entry pointing at a deleted executable.

So every claim the scheme makes is a test:

  * every ProgID the installer offers is registered AND unregistered
  * every registry key the installer writes is deleted by the uninstaller
  * the two wizards agree on the product's identity, to the character
  * the preserve list has exactly one home
  * progress weights actually sum to a full bar
  * the wizards import nothing they will not have when frozen
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import install as installer_module  # noqa: E402
import uninstall as uninstaller_module  # noqa: E402
from lumen_reader import version as version_module  # noqa: E402

REGISTER_PS1 = (ROOT / "register_associations.ps1").read_text(encoding="utf-8")
UNREGISTER_PS1 = (ROOT / "unregister_associations.ps1").read_text(encoding="utf-8")
CREATE_PS1 = (ROOT / "CreateShortcut.ps1").read_text(encoding="utf-8")
REMOVE_PS1 = (ROOT / "RemoveShortcut.ps1").read_text(encoding="utf-8")
PRESERVE = json.loads((ROOT / "preserved_user_state.json").read_text(encoding="utf-8"))

ALL_PS1 = {
    "register_associations.ps1": REGISTER_PS1,
    "unregister_associations.ps1": UNREGISTER_PS1,
    "CreateShortcut.ps1": CREATE_PS1,
    "RemoveShortcut.ps1": REMOVE_PS1,
}


# ── The wizards describe the same product ───────────────────────────────────

@pytest.mark.parametrize("name", [
    "PRODUCT_NAME", "FOLDER_NAME", "EXE_NAME", "ICON_NAME",
    "ARP_KEY", "DISCOVERY_KEY", "MANIFEST_NAME",
])
def test_installer_and_uninstaller_agree_on_identity(name):
    """A single mismatched constant means the uninstaller cleans nothing."""
    assert getattr(installer_module, name) == getattr(uninstaller_module, name), (
        f"install.py and uninstall.py disagree about {name}"
    )


def test_registry_keys_written_are_also_deleted():
    """Every HKCU key the installer creates must be named by the uninstaller."""
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    for key in (installer_module.ARP_KEY, installer_module.DISCOVERY_KEY):
        assert "_delete_registry_tree(ARP_KEY)" in source or key in source
    assert "_delete_registry_tree(ARP_KEY)" in source
    assert "_delete_registry_tree(DISCOVERY_KEY)" in source


# ── File associations: offered, registered, unregistered ────────────────────

def test_every_offered_type_is_registered_and_unregistered():
    for ext, _label, prog_id, _default in installer_module.ASSOCIATIONS:
        assert f"Ext          = '{ext}'" in REGISTER_PS1 or f"Ext = '{ext}'" in REGISTER_PS1, (
            f"{ext} is offered in the installer but absent from the registrar"
        )
        assert f"Ext = '{ext}'" in UNREGISTER_PS1 or f"Ext          = '{ext}'" in UNREGISTER_PS1, (
            f"{ext} is registered but never unregistered - it would outlive the app"
        )
        assert prog_id in REGISTER_PS1, f"{prog_id} missing from the registrar"
        assert prog_id in UNREGISTER_PS1, f"{prog_id} missing from the unregistrar"


def test_winreg_fallback_covers_every_progid():
    """The Python fallback runs when PowerShell is blocked; it must be complete."""
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    fallback = source.split("_purge_associations_via_winreg", 1)[1]
    for _ext, _label, prog_id, _default in installer_module.ASSOCIATIONS:
        assert prog_id in fallback, (
            f"{prog_id} is not cleared by the winreg fallback, so a machine with "
            f"PowerShell blocked would keep a dead association forever"
        )
    for ext, _l, _p, _d in installer_module.ASSOCIATIONS:
        assert f'"{ext}"' in fallback


def test_registrar_writes_every_surface_the_unregistrar_clears():
    """The five registry surfaces are a package deal - all five, or none work."""
    surfaces = [
        "Software\\Classes\\Applications\\",          # Open-with entry
        "RegisteredApplications",                     # Default Programs
        "Capabilities",                               # Default Programs
        "App Paths",                                  # Win+R / ShellExecute
        "OpenWithProgids",                            # per-extension menu
    ]
    for surface in surfaces:
        assert surface in REGISTER_PS1, f"registrar never writes {surface}"
        assert surface in UNREGISTER_PS1, f"unregistrar never clears {surface}"


def test_default_is_only_claimed_when_the_user_asks():
    """Ticking a format must not silently steal .pdf from the user's default app."""
    assert "$setDefault" in REGISTER_PS1
    # The (Default) value under the extension key is written INSIDE the
    # SetAsDefault branch, never unconditionally.
    branch = REGISTER_PS1.split("if ($setDefault) {", 1)
    assert len(branch) == 2, "the SetAsDefault branch has disappeared"
    before_branch = branch[0]
    assert "Set-ItemProperty -Path $extKey -Name '(Default)'" not in before_branch, (
        "the registrar claims the file-type default outside the SetAsDefault "
        "branch - ticking .pdf would take it from Acrobat without being asked"
    )


def test_unregistrar_never_deletes_a_shared_extension_key_outright():
    """Deleting HKCU:\\Software\\Classes\\.pdf wholesale breaks other apps."""
    assert "Remove-Item -Path $extKey -Recurse" not in UNREGISTER_PS1
    # It may remove the key only after proving it is empty.
    assert "Removed the now-empty" in UNREGISTER_PS1


def test_powershell_never_assigns_to_the_read_only_pid_variable():
    """$pid is a PowerShell automatic variable; assigning to it aborts the script."""
    for name, text in ALL_PS1.items():
        assert not re.search(r"foreach\s*\(\s*\$pid\b", text, re.IGNORECASE), (
            f"{name} loops over $pid, which is read-only and will throw"
        )


def test_shortcut_scripts_mirror_each_other():
    """Every name CreateShortcut.ps1 can write, RemoveShortcut.ps1 must remove."""
    written = set(re.findall(r'"(Lumen[^"]*\.lnk)"', CREATE_PS1))
    removed = set(re.findall(r'"(Lumen[^"]*\.lnk)"', REMOVE_PS1))
    assert written, "CreateShortcut.ps1 writes no .lnk at all"
    assert written <= removed, (
        f"these shortcuts are created but never removed: {sorted(written - removed)}"
    )


def test_shortcuts_start_in_the_library_not_the_install_folder():
    """A shortcut rooted at the install folder opens an empty shelf and drops
    the reader's marks among the program files."""
    assert "LibraryDir" in CREATE_PS1
    assert "WorkingDirectory" in CREATE_PS1
    assert "$workingDir" in CREATE_PS1


# ── Preserved user state ────────────────────────────────────────────────────

def test_preserve_list_has_one_home_and_the_fallbacks_agree():
    shared = {n.lower() for n in PRESERVE["preserve_on_reinstall"]}
    fallback = {n.lower() for n in installer_module.LumenInstaller._PRESERVE_FALLBACK}
    assert fallback <= shared, (
        "install.py's built-in fallback protects names the shared list does not: "
        f"{sorted(fallback - shared)}"
    )
    keep = {n.lower() for n in PRESERVE["keep_on_uninstall"]}
    keep_fallback = {n.lower() for n in uninstaller_module.LumenUninstaller._KEEP_FALLBACK}
    assert keep_fallback <= keep


def test_preserved_member_matching_is_top_level_and_existence_gated(tmp_path):
    match = installer_module.LumenInstaller._is_preserved_member
    preserved = {"logs", "lumeninstall.json"}
    target = str(tmp_path)

    # Nothing on disk yet: a FRESH install must receive every seed file.
    assert match("logs/lumen.log", target, preserved) is False

    (tmp_path / "logs").mkdir()
    assert match("logs/lumen.log", target, preserved) is True
    assert match("logs", target, preserved) is True
    assert match("_internal/base_library.zip", target, preserved) is False


def test_appdata_dir_matches_the_applications_organisation_name():
    """QApplication.setOrganizationName decides where reading state lives; the
    uninstaller must offer to clear that exact folder."""
    app_source = (ROOT / "lumen_reader" / "app.py").read_text(encoding="utf-8")
    org = re.search(r'setOrganizationName\("([^"]+)"\)', app_source)
    assert org, "app.py no longer sets an organisation name"
    assert org.group(1) == uninstaller_module.APPDATA_DIR_NAME
    assert org.group(1) in PRESERVE["appdata_dirs"]


# ── Progress bars that actually reach 100% ──────────────────────────────────

@pytest.mark.parametrize("cls", [
    installer_module.LumenInstaller,
    uninstaller_module.LumenUninstaller,
])
def test_step_weights_sum_to_a_full_bar(cls):
    total = sum(weight for _desc, weight in cls.STEPS)
    assert total == pytest.approx(1.0, abs=1e-9), (
        f"{cls.__name__}.STEPS weights sum to {total}; the progress bar would "
        f"{'stop short of' if total < 1 else 'overshoot'} 100%"
    )


@pytest.mark.parametrize("cls", [
    installer_module.LumenInstaller,
    uninstaller_module.LumenUninstaller,
])
def test_every_step_has_a_positive_weight(cls):
    for desc, weight in cls.STEPS:
        assert weight > 0, f"{cls.__name__} step {desc!r} has no weight"


# ── The wizards must survive being frozen ───────────────────────────────────

@pytest.mark.parametrize("filename", ["install.py", "uninstall.py"])
def test_wizards_import_only_the_standard_library(filename):
    """A frozen wizard has no access to lumen_reader or to third-party packages.

    Importing one would work perfectly on Angela's machine and fail on every
    user's, at the worst possible moment.
    """
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    forbidden = {"lumen_reader", "PySide6", "fitz", "bs4", "nltk", "versioning",
                 "build_support"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for name in names:
            assert name not in forbidden, (
                f"{filename} imports {name!r}, which will not exist in the frozen "
                f"wizard"
            )


@pytest.mark.parametrize("filename", ["install.py", "uninstall.py"])
def test_wizards_resolve_their_version_from_their_own_versioninfo(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    assert "_read_exe_product_version" in source
    assert "ProductVersion" in source


# ── Versioning ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,part,expected", [
    ("1.1.0", "patch", "1.1.1"),
    ("1.1.0", "minor", "1.2.0"),
    ("1.1.0", "major", "2.0.0"),
    ("1.9.9", "minor", "1.10.0"),
    ("0.0.0", "major", "1.0.0"),
    ("2.3.4-rc1", "patch", "2.3.5"),
])
def test_bump(current, part, expected):
    assert version_module.bump(current, part) == expected


def test_bump_rejects_nonsense():
    with pytest.raises(ValueError):
        version_module.bump("1.1.0", "sideways")
    with pytest.raises(ValueError):
        version_module.bump("not-a-version", "minor")


@pytest.mark.parametrize("left,right,expected", [
    ("1.0.0", "1.0.0", 0),
    ("1.0.1", "1.0.0", 1),
    ("1.0.0", "1.1.0", -1),
    ("2.0.0", "1.99.99", 1),
    ("1.0.0", "1.0.0-rc1", 1),     # a release outranks its own pre-release
    ("1.0.0-rc1", "1.0.0-rc2", -1),
])
def test_compare(left, right, expected):
    assert version_module.compare(left, right) == expected


def test_semver_to_win32_tuple():
    assert version_module.semver_to_win32_tuple("1.2.3") == (1, 2, 3, 0)
    assert version_module.semver_to_win32_tuple("1.2.3-rc1+build.7") == (1, 2, 3, 0)
    assert version_module.semver_to_win32_tuple("garbage") == (0, 0, 0, 0)


def test_versioninfo_carries_the_version_and_the_author():
    rendered = version_module.render_pyinstaller_version_file("3.4.5")
    assert "filevers=(3, 4, 5, 0)" in rendered
    assert "u'ProductVersion',   u'3.4.5'" in rendered
    assert version_module.AUTHOR in rendered
    assert "Lumen" in rendered


def test_get_version_is_a_real_semver():
    resolved = version_module.get_version()
    assert version_module.parse_semver(version_module.public_version(resolved)), (
        f"get_version() returned {resolved!r}, which is not parseable SemVer"
    )


def test_declared_version_matches_the_package_metadata():
    """pyproject.toml is the floor the resolver falls back to; keep it real."""
    declared = version_module.declared_version()
    assert declared, "pyproject.toml declares no version"
    assert version_module.parse_semver(declared)


# ── The build scripts describe a package the installer can actually use ─────

def test_build_ships_every_file_the_wizards_look_for():
    import build

    shipped = set(build.SUPPORT_FILES)
    needed = {
        "CreateShortcut.ps1", "RemoveShortcut.ps1",
        "register_associations.ps1", "unregister_associations.ps1",
        "preserved_user_state.json",
    }
    assert needed <= shipped, f"pkg.zip would not contain: {sorted(needed - shipped)}"
    for name in shipped:
        assert (ROOT / name).is_file(), f"build.py ships {name}, which does not exist"


def test_package_verification_requires_the_executable():
    """build.py must refuse to ship a package without Lumen.exe in it."""
    source = (ROOT / "build.py").read_text(encoding="utf-8")
    verify = source.split("def verify_pkg_zip", 1)[1]
    assert 'f"{APP_NAME}.exe"' in verify
    assert "_internal/" in verify
    assert "ABORT" in verify


def _git_argument_strings(path: Path) -> list[list[str]]:
    """Every argument list handed to a subprocess or to the ``_git`` helper.

    Scanning the raw text would trip over the docstring that PROMISES we never
    rebase, so we look at what the code actually executes instead of at what it
    says about itself.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[list[str]] = []

    def literals(node) -> list[str]:
        out: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                out.append(child.value)
        return out

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name in {"_git", "run", "Popen", "check_output", "check_call"}:
            calls.append([s for arg in node.args for s in literals(arg)])
    return calls


# The complete set of git subcommands the release pipeline is allowed to run.
# "tag" is here because ADDING a tag is forward-only; everything that could
# move or erase history is absent, and this list is the enforcement.
_ALLOWED_GIT_SUBCOMMANDS = {"rev-parse", "status", "tag", "describe", "log",
                            "show", "rev-list"}
_HISTORY_REWRITING = {"rebase", "commit", "reset", "filter-branch",
                      "filter-repo", "push", "checkout", "restore", "clean"}


def test_release_orchestrator_only_runs_read_only_git_plus_adding_a_tag():
    """The Private Data Guard, as a test - checked against real invocations."""
    path = ROOT / "build_complete_release.py"
    inspected = 0
    for args in _git_argument_strings(path):
        if not args:
            continue                      # a call with no literal arguments
        subcommand = args[1] if (args[0] == "git" and len(args) > 1) else args[0]
        inspected += 1
        if subcommand not in _ALLOWED_GIT_SUBCOMMANDS:
            # Not a git call at all (e.g. the sub-build invocations).
            assert subcommand not in _HISTORY_REWRITING, (
                f"build_complete_release.py runs git {subcommand!r}, which can "
                f"rewrite history"
            )
            continue
        flags = set(args)
        assert "-d" not in flags and "--delete" not in flags, (
            f"build_complete_release.py deletes a git ref: {args}"
        )
        assert "-f" not in flags and "--force" not in flags, (
            f"build_complete_release.py forces a git operation: {args}"
        )
        assert "--amend" not in flags
    assert inspected, "no subprocess calls were inspected - the scan is broken"


def test_release_orchestrator_refuses_to_move_an_existing_tag():
    source = (ROOT / "build_complete_release.py").read_text(encoding="utf-8")
    assert "tag_exists" in source
    assert "already exists" in source
    assert "REFUSING" in source
    # No CLI escape hatch that would let someone force it.
    assert '"--force"' not in source
    assert "'--force'" not in source


def test_launcher_anchors_the_working_directory_to_the_book():
    """Opening a book from Explorer must not scatter marks into system32."""
    source = (ROOT / "lumen_reader" / "launcher.py").read_text(encoding="utf-8")
    assert "os.chdir" in source
    assert "is_supported_book" in source


def test_frozen_entry_point_is_importable_without_package_context():
    """PyInstaller runs the entry script as __main__ with NO package context.

    Handing it ``lumen_reader/launcher.py`` would blow up on the first relative
    import, at runtime, on the user's machine. The entry point must therefore be
    a top-level module using absolute imports.
    """
    import build

    entry = build.ENTRY_POINT
    assert entry.is_file(), f"build.py's entry point {entry} does not exist"
    assert entry.parent == ROOT, (
        f"the entry point lives in {entry.parent.name}/ - PyInstaller would "
        f"anchor its search path there instead of at the repo root"
    )
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"{entry.name} uses a relative import, which raises "
                f"'attempted relative import with no known parent package' "
                f"the moment PyInstaller runs it"
            )
    # And the shim must actually reach the launcher.
    assert "lumen_reader.launcher" in entry.read_text(encoding="utf-8")


def test_pyinstaller_is_told_where_the_package_lives():
    source = (ROOT / "build.py").read_text(encoding="utf-8")
    assert '"--paths", str(ROOT)' in source


def test_the_scientific_stack_is_excluded_and_unused():
    """The bundle must not carry torch, pandas, scipy or scikit-learn.

    Importing ``nltk.corpus`` pulls all four, and PyInstaller's hooks then
    follow them into torchvision, torchaudio, av and soundfile - gigabytes of
    payload for a book reader that imports none of it. The exclusion is only
    safe while Lumen genuinely does not use them, so this test checks BOTH
    halves: that the build excludes them, and that no Lumen module imports one.
    """
    import build

    heavy = {"torch", "torchvision", "torchaudio", "pandas", "numpy", "scipy",
             "sklearn", "matplotlib", "av", "soundfile", "sqlalchemy"}
    excluded = set(build.EXCLUDES)
    assert heavy <= excluded, (
        f"these would be bundled: {sorted(heavy - excluded)}"
    )

    for module in (ROOT / "lumen_reader").glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert name not in heavy, (
                    f"{module.name} imports {name!r}, which build.py excludes - "
                    f"the frozen app would crash on that import"
                )


def test_nltk_is_not_collected_wholesale():
    """--collect-all nltk is the specific mistake that caused the bloat."""
    source = (ROOT / "build.py").read_text(encoding="utf-8")
    assert '"--collect-all", "nltk"' not in source, (
        "build.py collects the whole of NLTK again; that walks into the "
        "scikit-learn and torch integrations and multiplies the payload"
    )
    assert '"--collect-submodules", "nltk.corpus"' in source


# ── The registry must not promise what the code cannot do ───────────────────

def test_quiet_uninstall_string_is_backed_by_a_real_silent_mode():
    """install.py advertises `"Uninstaller.exe" /S` to Windows and winget.

    Those tools take the value at its word and expect no UI. Advertising a
    capability we do not have would be a lie told by the registry.
    """
    install_source = (ROOT / "install.py").read_text(encoding="utf-8")
    assert "QuietUninstallString" in install_source

    # Pull the switch straight out of the value the installer writes, so the
    # test tracks the code rather than a hard-coded guess about it.
    advertised = ""
    lines = install_source.splitlines()
    for i, line in enumerate(lines):
        if "QuietUninstallString" not in line:
            continue
        for candidate in (line, *lines[i + 1:i + 3]):
            for token in candidate.replace('"', " ").split():
                if token.startswith("/") and token[1:].isalpha():
                    advertised = token.upper()
                    break
            if advertised:
                break
        break
    assert advertised, "could not find the switch advertised in QuietUninstallString"

    assert hasattr(uninstaller_module, "run_silent"), (
        "uninstall.py has no run_silent(); the QuietUninstallString would open a "
        "window in an unattended run"
    )
    uninstall_source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    dispatch = uninstall_source.split("def main()", 1)[1]
    assert advertised in dispatch.upper(), (
        f"the ARP entry advertises {advertised} but main() never dispatches on it"
    )
    assert "run_silent" in dispatch


def test_silent_mode_never_deletes_reading_data_by_default():
    """An unattended run must not destroy something a person would be asked about.

    Uninstalling is now total - configuration, index, caches and registry all
    go without asking - so this invariant moved rather than disappearing: the
    reading data is EXPORTED first, and losing it requires an explicit
    ``/NOSAVE``. The guard is the same promise against a different mechanism.
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    silent = source.split("def run_silent", 1)[1].split("class _ConstantFlag", 1)[0]
    assert "NOSAVE" in silent, "there is no opt-out flag for erasing reading data"
    assert "skip_save = any(" in silent
    assert "SAVETO=" in silent, "an unattended run cannot choose where to save"
    # Opting out has to be deliberate: the default must be to save.
    assert "skip_save_var = _ConstantFlag(skip_save)" in silent
    # And it must refuse a folder it cannot identify as Lumen.
    assert "REFUSING" in silent
    assert "looks_right" in silent


def test_uninstall_exports_reading_data_before_it_deletes_anything():
    """Order is the whole safety property: ASK, EXPORT, VERIFY, THEN ERASE.

    If the export ever slides below the first deletion, a failed write hands
    the user an empty file after their history is already gone - the one
    outcome in this wizard that cannot be undone.
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    runner = source.split("def _run_uninstall", 1)[1].split("def _keep_names", 1)[0]

    export_at = runner.index("_export_reading_data")
    for destructive in ("RemoveShortcut.ps1", "_remove_files", "_erase_all_traces",
                        "_delete_registry_tree"):
        assert export_at < runner.index(destructive), (
            f"{destructive} runs before the reading-data export; a failed "
            f"export would then destroy the only copy"
        )

    # The export must be read back before it is trusted.
    exporter = source.split("def _export_reading_data", 1)[1].split("\n    # ─", 1)[0]
    assert "_read_json(path)" in exporter, "the export is never verified on disk"
    assert "raise OSError" in exporter, "a bad export must abort the uninstall"


def test_total_erasure_covers_every_surface_the_installer_creates():
    """Whatever the installer writes, the eraser must name."""
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    eraser = source.split("def _erase_all_traces", 1)[1].split("@staticmethod", 1)[0]
    for surface in ("ARP_KEY", "DISCOVERY_KEY", "APPDATA_DIR_NAME",
                    "App Paths", "_purge_associations_via_winreg"):
        assert surface in eraser, f"total erasure never touches {surface}"
    # Books are the one thing it must not reach for.
    assert "_protects_export" in eraser, "the export can be erased with the state"


def test_silent_mode_refuses_an_unidentified_folder(tmp_path, capsys, monkeypatch):
    """The most dangerous path in the whole scheme, exercised for real."""
    victim = tmp_path / "Important Documents"
    victim.mkdir()
    (victim / "thesis.docx").write_text("years of work", encoding="utf-8")

    code = uninstaller_module.run_silent(
        ["Uninstaller.exe", f"/D={victim}", "/S"])

    assert code == 3, "run_silent did not refuse an unidentified folder"
    assert victim.is_dir()
    assert (victim / "thesis.docx").read_text(encoding="utf-8") == "years of work"


# ── Both wizards must be able to say what went wrong ────────────────────────

@pytest.mark.parametrize("filename", ["install.py", "uninstall.py"])
def test_wizards_log_to_a_file_because_frozen_windowed_apps_have_no_stdout(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    assert "redirect_output_to_log" in source
    assert "LOG_PATH" in source
    # The failure dialog must tell the user where the log is.
    assert "A full log was written to" in source


# ── Redirected profiles ─────────────────────────────────────────────────────

def test_default_library_uses_the_windows_known_folder_api():
    r"""%USERPROFILE%\Documents is a guess, and on a redirected profile it is
    the wrong one - the real folder can be under OneDrive, and can even be
    localised ("Documentos"). Ask Windows instead of guessing."""
    source = (ROOT / "install.py").read_text(encoding="utf-8")
    assert "SHGetKnownFolderPath" in source
    assert "FDD39AD0-238F-46AF-ADB4-6C85480369C7" in source  # FOLDERID_Documents

    resolved = installer_module.LumenInstaller._default_library()
    assert resolved, "no library default was resolved at all"
    assert os.path.isdir(resolved), f"the default library {resolved!r} does not exist"


def test_shortcut_creator_uses_the_shell_for_the_desktop_too():
    r"""Same trap on the PowerShell side: $env:USERPROFILE\Desktop would write
    the shortcut into a folder a OneDrive user never looks at."""
    assert '[Environment]::GetFolderPath("Desktop")' in CREATE_PS1
    assert '[Environment]::GetFolderPath("Programs")' in CREATE_PS1
    # Only CODE counts - the comment above that call names the trap on purpose.
    code = [ln for ln in CREATE_PS1.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("USERPROFILE" in ln for ln in code), (
        "CreateShortcut.ps1 builds a path from $env:USERPROFILE, which ignores "
        "folder redirection"
    )


# ── The destructive-New-Item trap ───────────────────────────────────────────
# `New-Item -Path <existing key> -Force` does NOT mean "create if missing" in
# the PowerShell registry provider: it DELETES the key and makes a new empty
# one, discarding its default value and every subkey. On a key Lumen owns that
# is intentional. On a key Lumen SHARES it is destruction, and it happened:
#   * HKCU:\Software\Classes\.pdf        -> erased another app's ProgID default
#   * ...\.pdf\OpenWithProgids            -> emptied every other app's entry
#   * HKCU:\Software\RegisteredApplications -> unregistered Telegram Desktop
# These three tests are the fence around that hole.

# Keys Lumen does NOT own. Creating any of them with -Force destroys another
# application's data.
SHARED_REGISTRY_KEYS = [
    "$extKey",
    "$owp",
    "$feKey",
    "$feOwp",
    "$registeredKey",
]


def _forced_new_item_targets(text: str) -> set[str]:
    """Every path passed to `New-Item ... -Force` in a PowerShell script."""
    targets = set()
    pattern = re.compile("New-Item\\s+-Path\\s+(\\S+)[^\\r\\n]*-Force")
    for match in pattern.finditer(text):
        targets.add(match.group(1).strip('"').strip("'"))
    return targets


def test_registrar_never_force_creates_a_shared_registry_key():
    forced = _forced_new_item_targets(REGISTER_PS1)
    clobbered = sorted(k for k in SHARED_REGISTRY_KEYS if k in forced)
    assert not clobbered, (
        "register_associations.ps1 calls New-Item -Force on shared keys "
        f"{clobbered} - that DELETES them, taking other applications' "
        "associations with it. Use New-SharedKey instead."
    )


def test_registrar_has_the_guarded_creation_helper_and_uses_it():
    assert "function New-SharedKey" in REGISTER_PS1, (
        "the guarded key-creation helper is gone"
    )
    for key in SHARED_REGISTRY_KEYS:
        assert f"New-SharedKey {key}" in REGISTER_PS1, (
            f"{key} is not created through New-SharedKey"
        )


def test_registrar_does_not_delete_the_discovery_key_it_was_just_given():
    r"""PHASE 1 must clear only the Capabilities subkey.

    install.py writes InstallLocation / Version / LibraryDir /
    RegisteredExtensions into HKCU\Software\XAIHT\Lumen Book Reader and then
    runs this script. Clearing that parent erased all of it seconds later,
    leaving a working installation no updater could find.
    """
    assert "Remove-KeyIfPresent $capRoot" not in REGISTER_PS1, (
        "the registrar deletes the whole discovery key, wiping the values "
        "install.py just wrote into it"
    )
    assert "Remove-KeyIfPresent $capKey" in REGISTER_PS1


def test_registrar_does_not_overwrite_another_apps_content_type():
    """A shared extension key's 'Content Type' belongs to whoever set it."""
    assert "existingType" in REGISTER_PS1
    assert "if (-not $existingType)" in REGISTER_PS1


def test_self_delete_hands_cmd_a_string_not_an_argument_list():
    r"""The detached self-delete must be launched with a STRING command line.

    ``subprocess.list2cmdline(["cmd", "/c", command])`` collapses the list into
    one quoted argument and escapes every inner quote as ``\"``, which is the
    MSVC runtime's convention and NOT cmd.exe's. cmd then receives mangled
    paths, matches no file, and exits 0 having deleted nothing - a silent no-op
    that looks exactly like a file-locking race and is not one. It fails under
    every combination of creation flags and succeeds under all of them once the
    command line is passed through verbatim, so the flags are a red herring and
    the argument form is the whole fix.

    The symptom this guards: Uninstaller.exe stranded in an otherwise empty
    installation folder, forever, on every machine.
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    popens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert popens, "uninstall.py no longer launches anything with subprocess.Popen"

    for call in popens:
        assert call.args, "subprocess.Popen called without a command"
        first = call.args[0]
        launches_cmd = (
            isinstance(first, ast.List)
            and first.elts
            and isinstance(first.elts[0], ast.Constant)
            and str(first.elts[0].value).lower() in ("cmd", "cmd.exe")
        )
        assert not launches_cmd, (
            "uninstall.py passes cmd.exe an ARGUMENT LIST. list2cmdline will "
            'escape the quoted paths as \\" and cmd.exe will silently delete '
            "nothing. Pass the command line as a single string instead."
        )


def test_self_delete_leaves_a_trace_because_it_outlives_the_process():
    """A detached helper that fails after we exit must not fail silently.

    The self-delete runs after the uninstaller is gone, so there is no window,
    no console and no exit code anyone will ever see. Without a log on disk a
    helper that deletes nothing is indistinguishable from one that works, which
    is precisely how this bug survived two rounds of fixing.
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    assert "lumen-self-delete.log" in source, (
        "the self-delete helper writes no trace - a silent failure after the "
        "process exits is unobservable and therefore untestable"
    )
    assert "SELF-DELETE OK" in source and "SELF-DELETE FAILED" in source, (
        "the trace does not record the OUTCOME, only that something ran"
    )


def test_self_delete_uses_plain_rmdir_so_kept_files_survive():
    """``rmdir`` must stay non-recursive.

    The uninstaller deliberately leaves the user's preserved files in place.
    ``rmdir /s`` would delete the folder and everything the user asked to keep;
    plain ``rmdir`` removes the folder only once it is genuinely empty, which
    is the intended meaning of "clean up after yourself".
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    # Comments are allowed to NAME the forbidden form in order to explain why
    # it is forbidden; only executable lines are searched. That means Python
    # comments AND the `rem` lines inside the embedded batch script.
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
        and not line.lstrip().lower().startswith("rem ")
    )
    assert "rmdir /s" not in code.lower(), (
        "rmdir /s would take the user's preserved files with the folder"
    )
    assert 'rmdir "%FOLDER%"' in code, "the folder is never cleaned up at all"


def test_self_delete_runs_from_a_script_not_a_single_cmd_line():
    r"""The retry logic must live in a batch FILE, one statement per line.

    On a single ``cmd /c`` command line, ``if exist X (...) & tail`` binds the
    tail to the IF. When the condition is false cmd discards everything after
    it, to the end of the line, and exits 0 - so a chain shaped

        del EXE & if exist EXE (retry) & rmdir FOLDER

    can only ever reach ``rmdir`` on the path where the delete FAILED. Deleting
    the exe successfully skipped the folder cleanup, which is why the folder
    outlived a self-delete that had, on its own terms, worked. Verified
    empirically: identical chains lose their tail whether the conditional body
    is a ping, a del, or an echo, and keep it when the conditional is removed.

    A batch file has no such rule - each line is parsed on its own.
    """
    source = (ROOT / "uninstall.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "lumen-self-delete.cmd" in code, (
        "the self-delete no longer runs from a script file"
    )
    # The killer shape: a conditional and a further command on the SAME line.
    offenders = [
        line.strip() for line in code.splitlines()
        if "if exist" in line and "&" in line.split("if exist", 1)[1]
    ]
    assert not offenders, (
        "a chained `if exist ... & ...` is back; when the condition is false "
        f"cmd throws away the rest of the line: {offenders}"
    )
