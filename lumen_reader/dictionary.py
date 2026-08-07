"""Dictionary response models and parsing helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


_WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class DictionaryEntry:
    word: str
    phonetic: str
    part_of_speech: str
    definition: str
    synonyms: tuple[str, ...]
    example: str = ""
    source: str = "DictionaryAPI.dev"

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "phonetic": self.phonetic,
            "part_of_speech": self.part_of_speech,
            "definition": self.definition,
            "synonyms": list(self.synonyms),
            "example": self.example,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DictionaryEntry":
        return cls(
            word=str(data.get("word") or "").strip(),
            phonetic=str(data.get("phonetic") or "").strip(),
            part_of_speech=str(data.get("part_of_speech") or "").strip(),
            definition=str(data.get("definition") or "").strip(),
            synonyms=tuple(str(item).strip() for item in data.get("synonyms") or [] if str(item).strip())[:6],
            example=str(data.get("example") or "").strip(),
            source=str(data.get("source") or "Dictionary cache").strip(),
        )


class DictionaryCache:
    """Small atomic cache so a successful lookup never needs the network twice."""

    MAX_ENTRIES = 2500

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, DictionaryEntry] = {}
        self.load()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.entries = {}
            return
        raw = payload.get("entries", {}) if isinstance(payload, dict) else {}
        parsed: dict[str, DictionaryEntry] = {}
        if isinstance(raw, dict):
            for word, data in raw.items():
                if not isinstance(data, dict):
                    continue
                try:
                    entry = DictionaryEntry.from_dict(data)
                except (TypeError, ValueError):
                    continue
                if entry.word and entry.definition:
                    parsed[str(word).casefold()] = entry
        self.entries = parsed

    def get(self, word: str) -> DictionaryEntry | None:
        return self.entries.get(word.casefold())

    def put(self, word: str, entry: DictionaryEntry) -> None:
        key = word.casefold()
        self.entries.pop(key, None)
        self.entries[key] = entry
        while len(self.entries) > self.MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": 1,
            "entries": {word: entry.to_dict() for word, entry in self.entries.items()},
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def normalize_lookup_word(selection: str) -> str | None:
    """Return one dictionary-safe word from a browser selection."""
    normalized = unicodedata.normalize("NFKC", selection).strip()
    if len(normalized) > 64:
        return None
    match = _WORD_RE.fullmatch(normalized)
    if not match:
        return None
    return normalized.replace("’", "'").casefold()


def normalize_lookup_text(selection: str) -> str | None:
    """Normalize either one word or a short, intentional multi-word selection."""
    normalized = " ".join(unicodedata.normalize("NFKC", selection).split())
    normalized = normalized.strip(" \t\r\n.,;:!?()[]{}\"“”")
    if not normalized or len(normalized) > 180:
        return None
    word = normalize_lookup_word(normalized)
    if word is not None:
        return word
    tokens = normalized.split()
    if not 2 <= len(tokens) <= 24:
        return None
    if not all(_WORD_RE.fullmatch(token.strip(".,;:!?()[]{}\"“”")) for token in tokens):
        return None
    return normalized.replace("’", "'")


def selection_lookup_delay_ms(selection: str) -> int:
    """Return the deliberate hover delay for a normalized drag selection."""
    return 2000 if len(selection.split()) > 1 else 1000


def parse_dictionary_payload(payload: bytes | str) -> DictionaryEntry | None:
    """Extract one concise entry from a DictionaryAPI.dev response."""
    entries = parse_dictionary_entries(payload)
    return entries[0] if entries else None


def parse_dictionary_entries(payload: bytes | str, limit: int = 3) -> list[DictionaryEntry]:
    """Extract distinct senses without collapsing one meaning into another."""
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return []
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return []

    first = data[0]
    word = str(first.get("word") or "").strip()
    phonetic = str(first.get("phonetic") or "").strip()
    if not phonetic:
        for item in first.get("phonetics") or []:
            if isinstance(item, dict) and item.get("text"):
                phonetic = str(item["text"]).strip()
                break

    entries: list[DictionaryEntry] = []
    seen_definitions: set[str] = set()

    for meaning in first.get("meanings") or []:
        if not isinstance(meaning, dict):
            continue
        definitions = meaning.get("definitions") or []
        for definition in definitions[:2]:
            if not isinstance(definition, dict) or not definition.get("definition"):
                continue
            definition_text = str(definition["definition"]).strip()
            definition_key = _definition_key(definition_text)
            if definition_key in seen_definitions:
                continue
            seen_definitions.add(definition_key)
            candidates = list(meaning.get("synonyms") or []) + list(
                definition.get("synonyms") or []
            )
            synonyms: list[str] = []
            seen_synonyms: set[str] = set()
            for synonym in candidates:
                clean = str(synonym).strip()
                key = clean.casefold()
                if clean and key != word.casefold() and key not in seen_synonyms:
                    seen_synonyms.add(key)
                    synonyms.append(clean)
            entries.append(
                DictionaryEntry(
                    word=word,
                    phonetic=phonetic,
                    part_of_speech=str(meaning.get("partOfSpeech") or "").strip(),
                    definition=definition_text,
                    synonyms=tuple(synonyms[:6]),
                    example=str(definition.get("example") or "").strip(),
                    source="DictionaryAPI.dev · online",
                )
            )
            if len(entries) >= limit:
                return entries
    return entries


def parse_wiktionary_entries(
    payload: bytes | str, selected_text: str, limit: int = 3
) -> list[DictionaryEntry]:
    """Parse the structured English Wiktionary definition endpoint."""
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return []
    groups = data.get("en", []) if isinstance(data, dict) else []
    if not isinstance(groups, list):
        return []
    entries: list[DictionaryEntry] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        definitions: list[str] = []
        example = ""
        for item in group.get("definitions") or []:
            if not isinstance(item, dict):
                continue
            clean = _html_to_text(item.get("definition"))
            key = _definition_key(clean)
            if not clean or key in seen:
                continue
            seen.add(key)
            definitions.append(clean)
            examples = item.get("parsedExamples") or item.get("examples") or []
            if not example and isinstance(examples, list) and examples:
                first_example = examples[0]
                if isinstance(first_example, dict):
                    example = _html_to_text(first_example.get("example"))
                else:
                    example = _html_to_text(first_example)
            if len(definitions) >= 2:
                break
        if definitions:
            entries.append(
                DictionaryEntry(
                    word=selected_text,
                    phonetic="",
                    part_of_speech=str(group.get("partOfSpeech") or "").strip(),
                    definition="\n• ".join(definitions),
                    synonyms=(),
                    example=example,
                    source="Wiktionary · online",
                )
            )
        if len(entries) >= limit:
            break
    return entries


def parse_wikipedia_phrase(payload: bytes | str, selected_text: str) -> DictionaryEntry | None:
    """Extract a concise encyclopedia explanation for an exact phrase or redirect."""
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        return None
    page = pages[0]
    extract = " ".join(str(page.get("extract") or "").split())
    if page.get("missing") or not extract or "may refer to:" in extract[:180].casefold():
        return None
    if len(extract) > 700:
        shortened = extract[:700].rsplit(" ", 1)[0].rstrip(" ,;:")
        extract = shortened + "…"
    return DictionaryEntry(
        word=selected_text,
        phonetic="",
        part_of_speech="phrase context",
        definition=extract,
        synonyms=(),
        source="Wikipedia phrase context · online",
    )


def parse_datamuse_phrase(payload: bytes | str, selected_text: str) -> DictionaryEntry | None:
    """Return one clearly labeled semantic interpretation of a complete phrase."""
    try:
        data: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(data, list):
        return None
    for item in data[:5]:
        if not isinstance(item, dict) or not item.get("defs"):
            continue
        raw_definition = str(item["defs"][0]).strip()
        part, separator, definition = raw_definition.partition("\t")
        if not separator:
            definition = part
            part = ""
        related = str(item.get("word") or "").strip()
        if related and related.casefold() != selected_text.casefold():
            definition = f"Related expression “{related}”: {definition.strip()}"
        return DictionaryEntry(
            word=selected_text,
            phonetic="",
            part_of_speech=part,
            definition=definition.strip(),
            synonyms=(),
            source="Datamuse phrase interpretation · online",
        )
    return None


def lookup_offline_wordnet(word: str, data_root: str | Path | None = None) -> DictionaryEntry | None:
    """Return a local Princeton WordNet definition, including inflected forms."""
    entries = lookup_offline_wordnet_entries(word, data_root=data_root, limit=1)
    return entries[0] if entries else None


def lookup_offline_wordnet_entries(
    word: str, data_root: str | Path | None = None, limit: int = 2
) -> list[DictionaryEntry]:
    """Return separate WordNet senses so later sources can be appended safely."""
    try:
        import nltk

        root = (
            Path(data_root)
            if data_root is not None
            else Path(__file__).resolve().parent / "assets" / "nltk_data"
        )
        root_text = str(root.resolve())
        if root_text not in nltk.data.path:
            nltk.data.path.insert(0, root_text)
        from nltk.corpus import wordnet

        candidates = [word]
        if word.endswith("'s") and len(word) > 2:
            candidates.append(word[:-2])
        synsets = []
        for candidate in candidates:
            query = candidate.replace("-", "_").replace(" ", "_")
            synsets = wordnet.synsets(query)
            if synsets:
                break
    except (ImportError, LookupError, OSError):
        return []
    if not synsets:
        return []

    part_names = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}
    base = str(wordnet.morphy(query) or query).replace("_", " ").casefold()
    entries: list[DictionaryEntry] = []
    seen_definitions: set[str] = set()
    for synset in synsets:
        definition = str(synset.definition()).strip()
        key = _definition_key(definition)
        if not definition or key in seen_definitions:
            continue
        seen_definitions.add(key)
        definition = definition[0].upper() + definition[1:]
        if definition[-1] not in ".!?":
            definition += "."
        synonyms: list[str] = []
        seen: set[str] = {word.casefold(), base}
        for lemma in synset.lemmas():
            clean = str(lemma.name()).replace("_", " ").strip()
            synonym_key = clean.casefold()
            if clean and synonym_key not in seen:
                seen.add(synonym_key)
                synonyms.append(clean)
        examples = list(synset.examples())
        entries.append(
            DictionaryEntry(
                word=word,
                phonetic="",
                part_of_speech=part_names.get(str(synset.pos()), ""),
                definition=definition,
                synonyms=tuple(synonyms[:6]),
                example=str(examples[0]).strip() if examples else "",
                source="Princeton WordNet 3.0 · offline",
            )
        )
        if len(entries) >= limit:
            break
    return entries


def _html_to_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())


def _definition_key(definition: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", definition.casefold()).strip()
