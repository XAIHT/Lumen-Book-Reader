"""Offline semantic query expansion backed by Lumen's bundled WordNet corpus."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SemanticStatus:
    available: bool
    selected: bool
    backend: str
    model_id: str | None
    reason: str


def status() -> SemanticStatus:
    wordnet = _wordnet()
    if wordnet is None:
        return SemanticStatus(
            available=False,
            selected=False,
            backend="none",
            model_id=None,
            reason="The bundled offline WordNet corpus could not be loaded; lexical FTS5 remains active.",
        )
    return SemanticStatus(
        available=True,
        selected=True,
        backend="wordnet-query-expansion",
        model_id="Princeton WordNet 3.0",
        reason="Offline semantic expansion is active; no network model call is used.",
    )


def expand_terms(query: str, maximum: int = 12) -> list[str]:
    """Return deterministic, bounded WordNet lemmas for a human query."""
    wordnet = _wordnet()
    if wordnet is None:
        return []
    originals = [item.casefold() for item in re.findall(r"\w{3,}", query, flags=re.UNICODE)]
    seen = set(originals)
    expanded: list[str] = []
    for word in originals[:6]:
        try:
            synsets = wordnet.synsets(word.replace("-", "_"))[:2]
        except (LookupError, OSError):
            continue
        for synset in synsets:
            for lemma in synset.lemmas()[:6]:
                value = " ".join(str(lemma.name()).replace("_", " ").split()).casefold()
                if len(value) < 3 or value in seen:
                    continue
                seen.add(value)
                expanded.append(value)
                if len(expanded) >= max(1, int(maximum)):
                    return expanded
    return expanded


@lru_cache(maxsize=1)
def _wordnet() -> Any | None:
    try:
        import nltk

        data_root = Path(__file__).resolve().parents[1] / "assets" / "nltk_data"
        root_text = str(data_root.resolve())
        if root_text not in nltk.data.path:
            nltk.data.path.insert(0, root_text)
        from nltk.corpus import wordnet

        wordnet.synsets("book")
        return wordnet
    except (ImportError, LookupError, OSError):
        return None
