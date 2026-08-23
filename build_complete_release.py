#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""build_complete_release.py - one command, one shippable archive.

    python build_complete_release.py                # version from the git tag
    python build_complete_release.py --bump minor   # 1.4.0 -> 1.5.0, tag it, build
    python build_complete_release.py --bump major   # 1.4.0 -> 2.0.0, tag it, build
    python build_complete_release.py --version 2.0.0-rc1

Pipeline
--------
  1. Resolve the version. ``--bump`` computes the next SemVer from the newest
     tag and, unless ``--no-tag`` is given, creates a NEW annotated git tag.
  2. ``build.py``             -> pkg.zip
  3. ``build_uninstaller.py`` -> Uninstaller.exe
  4. ``build_installer.py``   -> dist/Lumen_Release_v<version>/
  5. Write ``SHA256SUMS.txt`` and ``RELEASE_MANIFEST.json`` INTO that folder,
     so the checksums travel with the files they describe.
  6. Zip the folder to
     ``dist/Lumen_Release_v<version>_win11x64_<timestamp>.zip`` and write a
     ``.sha256`` sidecar for the archive itself.

PRIVATE DATA GUARD - THIS SCRIPT NEVER REWRITES HISTORY
--------------------------------------------------------
Tagging is the only git write it performs, and a tag is a FORWARD-ONLY
addition. There is no rebase, no amend, no reset, no force-push, no tag
deletion, and no ``--force`` flag to add one. If the tag it wants already
exists, it STOPS and says so rather than moving anything. ``git log`` stays
truthful, always.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from build_support import banner, sha256, step, utf8_env
from versioning import (
    ABOUT_URL,
    AUTHOR,
    COMPANY_NAME,
    PRODUCT_NAME,
    bump as bump_version,
    declared_version,
    derive_version_from_git,
    git_commit,
    parse_semver,
    resolve_build_version,
    safe_version_for_path,
    warn_if_tag_behind,
)

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build.py"
BUILD_UNINST = ROOT / "build_uninstaller.py"
BUILD_INST = ROOT / "build_installer.py"

# Never checksummed into SHA256SUMS.txt: a file cannot list its own digest.
SUMS_NAME = "SHA256SUMS.txt"
MANIFEST_NAME = "RELEASE_MANIFEST.json"


# ── Git: forward-only ──────────────────────────────────────────────────────

def _git(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, check=check)


def tag_exists(tag: str) -> bool:
    result = _git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"])
    return result.returncode == 0 and bool(result.stdout.strip())


def working_tree_dirty() -> bool:
    result = _git(["status", "--porcelain"])
    return bool((result.stdout or "").strip())


def create_tag(version: str, allow_dirty: bool) -> None:
    """Create a NEW annotated tag. Never moves or deletes an existing one."""
    tag = f"v{version}"
    step(f"Tagging the release as {tag}")

    if tag_exists(tag):
        sys.exit(
            f"REFUSING: the tag {tag} already exists.\n"
            f"  This script never moves or deletes a tag - history stays truthful.\n"
            f"  Either build that existing version:\n"
            f"      python build_complete_release.py --version {version}\n"
            f"  or choose the next one:\n"
            f"      python build_complete_release.py --bump patch"
        )

    if working_tree_dirty() and not allow_dirty:
        sys.exit(
            "REFUSING: the working tree has uncommitted changes.\n"
            f"  A tag would point at a commit that does not contain what you are\n"
            f"  about to build, and the release would misreport its own source.\n"
            f"  Commit first, or pass --allow-dirty if you know what you are doing."
        )

    result = _git(["tag", "-a", tag, "-m", f"{PRODUCT_NAME} {version}"])
    if result.returncode != 0:
        sys.exit(f"ERROR: could not create the tag {tag}:\n{result.stderr}")
    print(f"  Created annotated tag {tag} at {git_commit()}")
    print(f"  Push it when you are ready:  git push origin {tag}")


# ── Sub-builds ─────────────────────────────────────────────────────────────

def run_stage(label: str, script: Path, version: str, python: str) -> None:
    banner(label)
    cmd = [python, str(script), "--version", version]
    print(f"$ {' '.join(cmd)}", flush=True)
    # LUMEN_VERSION is already exported by build.py's emit_build_artifacts, but
    # setting it here means every stage agrees even if a stage is skipped.
    env = utf8_env()
    env["LUMEN_VERSION"] = version
    if subprocess.run(cmd, cwd=str(ROOT), env=env).returncode != 0:
        sys.exit(f"{script.name} FAILED - no release produced.")


def newest_release_dir(version: str) -> Path | None:
    expected = DIST / f"Lumen_Release_v{safe_version_for_path(version)}"
    if expected.is_dir():
        return expected
    candidates = sorted(
        (p for p in DIST.glob("Lumen_Release_v*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return candidates[0] if candidates else None


# ── Provenance ─────────────────────────────────────────────────────────────

def write_checksums(release_dir: Path) -> dict[str, str]:
    """Hash every file in the release folder into ``SHA256SUMS.txt``.

    Standard ``sha256sum`` format, so a user can verify the download with the
    tool they already have:  ``sha256sum -c SHA256SUMS.txt``
    """
    step("Computing SHA-256 checksums")
    digests: dict[str, str] = {}
    files = sorted(p for p in release_dir.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(release_dir).as_posix()
        if rel in (SUMS_NAME, MANIFEST_NAME):
            continue          # cannot contain their own digest
        digests[rel] = sha256(path)

    lines = [f"{digest}  {name}" for name, digest in sorted(digests.items())]
    (release_dir / SUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {len(digests)} file(s) hashed -> {SUMS_NAME}")
    return digests


def write_manifest(release_dir: Path, version: str, digests: dict[str, str],
                   started: float) -> Path:
    """Machine-readable provenance for the release.

    Answers, months later and without guessing: which commit built this, on
    which Python, how big it is, and what it claims to register.
    """
    step("Writing the release manifest")
    parsed = parse_semver(version.split("+")[0]) or {}
    total = sum(p.stat().st_size for p in release_dir.rglob("*") if p.is_file())
    manifest = {
        "product": PRODUCT_NAME,
        "publisher": COMPANY_NAME,
        "author": AUTHOR,
        "url": ABOUT_URL,
        "version": version,
        "semver": {
            "major": parsed.get("major"),
            "minor": parsed.get("minor"),
            "patch": parsed.get("patch"),
            "prerelease": parsed.get("prerelease", ""),
        },
        "commit": git_commit(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "build_seconds": round(time.time() - started, 1),
        "build_host": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "registers_file_types": [".epub", ".pdf"],
        "registry_scope": "HKEY_CURRENT_USER (per-user, no administrator rights)",
        "total_bytes": total,
        "files": dict(sorted(digests.items())),
    }
    path = release_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"  {path.name} written ({total / (1024 * 1024):.1f} MB across "
          f"{len(digests)} files)")
    return path


def make_archive(release_dir: Path, version: str) -> Path:
    step("Packaging the distributable archive")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = DIST / f"{release_dir.name}_win11x64_{stamp}"
    archive = Path(shutil.make_archive(
        str(base), "zip", root_dir=str(DIST), base_dir=release_dir.name))
    digest = sha256(archive)
    (archive.parent / (archive.name + ".sha256")).write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"  {archive.name}  ({archive.stat().st_size / (1024 * 1024):.1f} MB)")
    print(f"  SHA-256: {digest}")
    return archive


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    started = time.time()
    ap = argparse.ArgumentParser(
        description=f"Build a complete, distributable {PRODUCT_NAME} release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="This script never rewrites git history. Tags are only ever ADDED.",
    )
    ap.add_argument("--version", default="",
                    help="explicit version (default: derived from the newest git tag)")
    ap.add_argument("--bump", choices=("major", "minor", "patch"),
                    help="compute the NEXT version from the newest tag and use it")
    ap.add_argument("--no-tag", action="store_true",
                    help="with --bump, do not create the git tag (build only)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow tagging with uncommitted changes in the tree")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter that drives the sub-builds")
    ap.add_argument("--skip-app", action="store_true",
                    help="reuse the existing pkg.zip instead of rebuilding it")
    ap.add_argument("--skip-uninstaller", action="store_true",
                    help="reuse the existing Uninstaller.exe")
    ap.add_argument("--no-archive", action="store_true",
                    help="stop after the release folder; do not zip it")
    args = ap.parse_args(argv)

    # ── Version ─────────────────────────────────────────────────────────
    if args.bump and args.version:
        sys.exit("Pass either --bump or --version, not both.")

    if args.bump:
        current = derive_version_from_git() or declared_version() or "0.0.0"
        version = bump_version(current, args.bump)
        print(f"Bumping {args.bump}: {current} -> {version}")
    else:
        version = resolve_build_version(args.version or None)

    if parse_semver(version.split("+")[0]) is None:
        sys.exit(f"REFUSING: {version!r} is not a valid SemVer version.")

    banner(f"{PRODUCT_NAME.upper()} RELEASE  ·  v{version}")
    print(f"repo        : {ROOT}")
    print(f"python      : {args.python}")
    print(f"newest tag  : {derive_version_from_git() or '(none)'}")
    print(f"pyproject   : {declared_version() or '(none)'}")
    print(f"commit      : {git_commit()}")

    if args.bump and not args.no_tag:
        create_tag(version, args.allow_dirty)
    else:
        warn_if_tag_behind(version)

    os.environ["LUMEN_VERSION"] = version

    # ── Stages ──────────────────────────────────────────────────────────
    if args.skip_app:
        if not (ROOT / "pkg.zip").is_file():
            sys.exit("--skip-app was given but pkg.zip is not at the repo root.")
        print("\nSkipping build.py - reusing the existing pkg.zip.")
    else:
        run_stage("STAGE 1/3  build.py  (freeze Lumen -> pkg.zip)", BUILD,
                  version, args.python)

    if args.skip_uninstaller:
        if not (ROOT / "Uninstaller.exe").is_file():
            sys.exit("--skip-uninstaller was given but Uninstaller.exe is not at "
                     "the repo root.")
        print("\nSkipping build_uninstaller.py - reusing the existing Uninstaller.exe.")
    else:
        run_stage("STAGE 2/3  build_uninstaller.py  (-> Uninstaller.exe)",
                  BUILD_UNINST, version, args.python)

    run_stage("STAGE 3/3  build_installer.py  (-> the release folder)",
              BUILD_INST, version, args.python)

    release_dir = newest_release_dir(version)
    if release_dir is None:
        sys.exit("ERROR: no dist/Lumen_Release_v* folder was produced.")

    # ── Provenance and packaging ────────────────────────────────────────
    banner("PROVENANCE")
    digests = write_checksums(release_dir)
    write_manifest(release_dir, version, digests, started)

    archive = None
    if not args.no_archive:
        banner("PACKAGING")
        archive = make_archive(release_dir, version)

    # ── Done ────────────────────────────────────────────────────────────
    banner(f"RELEASE COMPLETE  ·  v{version}  ·  {time.time() - started:.0f}s")
    print(f"  release folder : {release_dir}")
    if archive:
        print(f"  distributable  : {archive}")
        print(f"  checksum       : {archive}.sha256")
    print(f"  manifest       : {release_dir / MANIFEST_NAME}")
    print(f"  checksums      : {release_dir / SUMS_NAME}")
    if args.bump and not args.no_tag:
        print(f"\n  Tag created locally. Publish it with:  git push origin v{version}")
    print("\n  Hand the user the zip. They unpack it and double-click Installer.exe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
