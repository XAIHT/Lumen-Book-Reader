"""Contextual and expert fallbacks for words absent from conventional dictionaries."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .dictionary import DictionaryEntry, lookup_offline_wordnet_entries


DEFAULT_GOOGLER_PATH = Path(r"C:\Tlamatini\agents\googler")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "glm-5.2:cloud"

_PYTHON_EXECUTABLE_PATTERN = re.compile(
    r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?", re.IGNORECASE
)

_YOUNG_ANIMALS = {
    "kitten": "cat",
    "puppy": "dog",
    "calf": "cow",
    "cub": "bear",
    "duckling": "duck",
    "gosling": "goose",
    "foal": "horse",
    "piglet": "pig",
    "chick": "chicken",
}


def default_definition_fallbacks() -> dict[str, Any]:
    """Return fresh persisted defaults for the optional deep-definition engines."""
    return {
        "contextual_inference": True,
        "googler_enabled": DEFAULT_GOOGLER_PATH.joinpath("googler.py").is_file(),
        "googler_path": str(DEFAULT_GOOGLER_PATH),
        "ollama_enabled": False,
        "ollama_url": DEFAULT_OLLAMA_URL,
        "ollama_model": DEFAULT_OLLAMA_MODEL,
    }


def normalized_ollama_url(base_url: str, endpoint: str = "") -> str:
    """Normalize either an Ollama host URL or a pasted API URL."""
    base = str(base_url or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    suffix = endpoint.strip("/")
    return f"{base}/api/{suffix}" if suffix else f"{base}/api"


def infer_contextual_entries(term: str, context: str) -> list[DictionaryEntry]:
    """Explain a plausible coined compound while labeling the inference honestly."""
    word = str(term or "").strip().casefold()
    passage = " ".join(str(context or "").split())[:1600]
    if " " in word or not word.isalpha() or not 6 <= len(word) <= 32:
        return []

    # Handle productive wordplay such as copycat -> copykitten. This is a
    # general adult/young-animal relationship, not a one-word special case.
    for young, adult in _YOUNG_ANIMALS.items():
        suffixes = ((young, False), (young + "s", True))
        if young.endswith("y"):
            suffixes = suffixes + ((young[:-1] + "ies", True),)
        for suffix, plural in suffixes:
            if not word.endswith(suffix) or len(word) <= len(suffix):
                continue
            prefix = word[: -len(suffix)]
            adult_form = prefix + adult + ("s" if plural else "")
            singular_adult = prefix + adult
            if passage and not re.search(
                rf"\b(?:{re.escape(adult_form)}|{re.escape(singular_adult)})\b",
                passage,
                re.IGNORECASE,
            ):
                continue
            number = "plural" if plural else "singular"
            definition = (
                f"A playful {number} diminutive of “{adult_form}”: small, young, "
                "or early-stage imitators rather than an established dictionary term. "
                "In this passage it labels the imitative people, products, or companies "
                "being discussed."
            )
            synonyms = [adult_form, "imitators"]
            if adult_form != "copycats":
                synonyms.append("copycats")
            return [
                DictionaryEntry(
                    word=term,
                    phonetic="",
                    part_of_speech="coined noun" + (" · plural" if plural else ""),
                    definition=definition,
                    synonyms=tuple(synonyms),
                    example=_context_example(passage, singular_adult),
                    source="Lumen contextual morphology · inferred",
                )
            ]

    # General fallback: accept a split only when both halves are independently
    # recognized by WordNet. Longer suffixes win.
    candidates: list[tuple[int, str, str, DictionaryEntry, DictionaryEntry]] = []
    for split in range(3, len(word) - 2):
        left, right = word[:split], word[split:]
        left_entries = lookup_offline_wordnet_entries(left, limit=1)
        right_entries = lookup_offline_wordnet_entries(right, limit=1)
        if left_entries and right_entries:
            candidates.append((len(right), left, right, left_entries[0], right_entries[0]))
    if not candidates:
        return []
    _, left, right, left_entry, right_entry = max(candidates, key=lambda item: item[0])
    definition = (
        f"A likely context-specific compound of “{left}” and “{right}”. “{left}” can mean "
        f"{_lower_first(left_entry.definition)}“{right}” can mean "
        f"{_lower_first(right_entry.definition)}The precise combined sense depends on "
        "the surrounding passage, so this interpretation is marked as inferred."
    )
    return [
        DictionaryEntry(
            word=term,
            phonetic="",
            part_of_speech="coined compound · inferred",
            definition=definition,
            synonyms=(),
            example=_context_example(passage, word),
            source="Lumen compound analysis · inferred",
        )
    ]


def build_ollama_chat_payload(
    term: str,
    context: str,
    model: str,
    *,
    book_title: str = "",
    chapter_title: str = "",
) -> dict[str, Any]:
    """Build a restrained lexicographer prompt for local or Ollama cloud models."""
    schema_instruction = (
        'Return only one JSON object with keys "definition", "part_of_speech", '
        '"synonyms" (an array of at most 6 strings), "base_form", and "confidence" '
        "(a number from 0 to 1)."
    )
    prompt = (
        "Act as a careful contextual lexicographer. Define the selected expression as it is "
        "used in the supplied book passage. It may be an inflection, pun, coined compound, "
        "proper noun, or phrase. Do not pretend it is a standard dictionary headword when it "
        "is not. Give a concise standalone definition and use the passage to disambiguate it.\n\n"
        f"Selected expression: {term}\n"
        f"Book: {book_title or 'unknown'}\n"
        f"Chapter: {chapter_title or 'unknown'}\n"
        f"Passage: {(' '.join(context.split())[:1800] or 'No passage was captured.')}\n\n"
        f"{schema_instruction}"
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    # Ollama Cloud does not guarantee structured-output mode, so the prompt
    # still requires JSON but the API format parameter is omitted there.
    if not model.casefold().endswith(":cloud"):
        payload["format"] = "json"
    return payload


def parse_ollama_chat_response(
    payload: bytes | str, term: str, model: str
) -> list[DictionaryEntry]:
    """Validate an Ollama chat result instead of rendering arbitrary model text."""
    try:
        outer = json.loads(payload)
        content = str(outer.get("message", {}).get("content") or "").strip()
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError, AttributeError):
        return []
    candidate = _extract_json_object(content)
    if not isinstance(candidate, dict):
        return []
    definition = " ".join(str(candidate.get("definition") or "").split())
    if not definition:
        return []
    definition = definition[:900].rstrip()
    synonyms: list[str] = []
    raw_synonyms = candidate.get("synonyms") or []
    if isinstance(raw_synonyms, list):
        for value in raw_synonyms:
            clean = " ".join(str(value).split())[:80]
            seen = {item.casefold() for item in synonyms}
            if clean and clean.casefold() != term.casefold() and clean.casefold() not in seen:
                synonyms.append(clean)
            if len(synonyms) >= 6:
                break
    base_form = " ".join(str(candidate.get("base_form") or "").split())[:80]
    if base_form and base_form.casefold() != term.casefold():
        definition = f"Base expression: “{base_form}”. {definition}"
    return [
        DictionaryEntry(
            word=term,
            phonetic="",
            part_of_speech=" ".join(str(candidate.get("part_of_speech") or "").split())[:80],
            definition=definition,
            synonyms=tuple(synonyms),
            source=f"Ollama · {model} · contextual AI",
        )
    ]


def resolve_tlamatini_python(agent_directory: str | Path) -> Path | None:
    """Find a real Python runtime for the isolated Tlamatini helper.

    ``sys.executable`` is Python while Lumen runs from source, but it is
    ``Lumen.exe`` in a PyInstaller build.  Reusing it there relaunches the
    reader instead of executing the helper bridge.  Frozen builds therefore
    use only a Python runtime installed beside the configured Tlamatini agent
    and fail closed when one is unavailable.
    """
    agent_dir = Path(agent_directory).expanduser().resolve()
    current_executable = Path(sys.executable).expanduser().resolve()
    candidates: list[Path] = []

    if not getattr(sys, "frozen", False):
        candidates.append(current_executable)

    roots = [agent_dir]
    if agent_dir.parent.name.casefold() == "agents":
        roots.append(agent_dir.parent.parent)
    else:
        roots.append(agent_dir.parent)

    for root in roots:
        candidates.extend(
            (
                root / "python" / "python.exe",
                root / ".venv" / "Scripts" / "python.exe",
                root / "venv" / "Scripts" / "python.exe",
                root / ".venv" / "bin" / "python",
                root / "venv" / "bin" / "python",
            )
        )

    seen: set[Path] = set()
    frozen = bool(getattr(sys, "frozen", False))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file() or not _PYTHON_EXECUTABLE_PATTERN.fullmatch(
            resolved.name
        ):
            continue
        if frozen and resolved == current_executable:
            continue
        return resolved
    return None


def run_tlamatini_googler(
    term: str, agent_directory: str | Path, timeout_seconds: float = 12.0
) -> list[DictionaryEntry]:
    """Run Tlamatini Googler in an isolated worker and return labeled web evidence.

    Importing Googler in-process would change Lumen's working directory, logging,
    environment, and global subprocess behavior. Isolation preserves both apps.
    """
    agent_dir = Path(agent_directory).expanduser().resolve()
    script_path = agent_dir / "googler.py"
    python_executable = resolve_tlamatini_python(agent_dir)
    clean_term = " ".join(str(term or "").split())[:240]
    if (
        not script_path.is_file()
        or python_executable is None
        or not clean_term
        or timeout_seconds <= 1
    ):
        return []
    query = f'"{clean_term}" meaning OR definition OR slang'
    bridge = r'''
import importlib.util, json, sys
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright
path, query = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("lumen_tlamatini_googler", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
results = []
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True, args=module._BROWSER_ARGS)
    context = browser.new_context(
        user_agent=module._USER_AGENT,
        viewport={"width": 1600, "height": 900},
        locale="en-US",
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = context.new_page()
    hits = []
    try:
        page.goto(
            "https://www.google.com/search?q=" + quote_plus(query),
            wait_until="domcontentloaded",
            timeout=5000,
        )
        module._dismiss_google_consent(page)
        page.wait_for_timeout(500)
        hits = module._extract_links_with_selectors(
            page, module._GOOGLE_RESULT_SELECTORS, allow_same_domain=True
        )
    except Exception:
        hits = []
    if not hits:
        try:
            page.goto(
                "https://duckduckgo.com/?q=" + quote_plus(query) + "&t=h_&ia=web",
                wait_until="domcontentloaded",
                timeout=5000,
            )
            page.wait_for_timeout(500)
            hits = module._extract_links_with_selectors(
                page,
                module._DDG_RESULT_SELECTORS,
                skip_domains={"duckduckgo.com"},
                allow_same_domain=True,
            )
        except Exception:
            hits = []
    for index, hit in enumerate(hits[:5], 1):
        results.append({
            "index": index,
            "url": hit.get("url", ""),
            "title": hit.get("title", ""),
            "status_code": "listed",
            "content_length": 0,
        })
    browser.close()
print("__LUMEN_GOOGLER_JSON__" + json.dumps(results, ensure_ascii=False))
'''
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(python_executable), "-c", bridge, str(script_path), query],
            cwd=str(agent_dir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, float(timeout_seconds)),
            creationflags=creationflags,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    marker = "__LUMEN_GOOGLER_JSON__"
    line = next((item for item in completed.stdout.splitlines() if item.startswith(marker)), "")
    if not line:
        return []
    try:
        results = json.loads(line[len(marker) :])
    except json.JSONDecodeError:
        return []
    return parse_tlamatini_results(clean_term, results)


def parse_tlamatini_results(term: str, results: Any) -> list[DictionaryEntry]:
    """Convert exact-search hits into a compact, transparent evidence card."""
    if not isinstance(results, list):
        return []
    exact = term.casefold()
    references: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        title = " ".join(str(result.get("title") or "").split())
        url = unquote(str(result.get("url") or ""))
        if exact not in title.casefold() and exact not in url.casefold():
            continue
        label = title[:170] or url[:170]
        if label and label.casefold() not in {item.casefold() for item in references}:
            references.append(label)
        if len(references) >= 3:
            break
    if not references:
        return []
    quoted = "”; “".join(references)
    definition = (
        f"Tlamatini found {len(references)} exact web reference"
        f"{'s' if len(references) != 1 else ''} for this expression: “{quoted}”. "
        "This corroborates that the wording is in real use, while the contextual or AI "
        "cards provide its intended meaning."
    )
    return [
        DictionaryEntry(
            word=term,
            phonetic="",
            part_of_speech="web evidence",
            definition=definition,
            synonyms=(),
            source="Tlamatini Googler · exact web evidence",
        )
    ]


def _extract_json_object(content: str) -> dict[str, Any] | None:
    fence = chr(96) * 3
    clean = content.strip()
    if clean.startswith(fence):
        clean = clean[len(fence) :].lstrip()
        if clean.casefold().startswith("json"):
            clean = clean[4:].lstrip()
        if clean.endswith(fence):
            clean = clean[: -len(fence)].rstrip()
    try:
        value = json.loads(clean)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(clean):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(clean[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _context_example(context: str, needle: str) -> str:
    if not context:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", context)
    for sentence in sentences:
        if needle.casefold() in sentence.casefold():
            return sentence[:300].strip()
    return context[:300].strip()


def _lower_first(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return "an unknown sense. "
    result = clean[0].lower() + clean[1:]
    return result + (" " if result[-1] in ".!?" else ". ")
