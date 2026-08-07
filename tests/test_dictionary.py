from __future__ import annotations

import json
from pathlib import Path

from lumen_reader.dictionary import (
    DictionaryCache,
    DictionaryEntry,
    lookup_offline_wordnet_entries,
    lookup_offline_wordnet,
    normalize_lookup_text,
    normalize_lookup_word,
    parse_datamuse_phrase,
    parse_dictionary_entries,
    parse_dictionary_payload,
    parse_wikipedia_phrase,
    parse_wiktionary_entries,
    selection_lookup_delay_ms,
)


def test_normalize_lookup_word_accepts_human_words_only() -> None:
    assert normalize_lookup_word("  Reader  ") == "reader"
    assert normalize_lookup_word("reader’s") == "reader's"
    assert normalize_lookup_word("well-being") == "well-being"
    assert normalize_lookup_word("two words") is None
    assert normalize_lookup_word("123") is None
    assert normalize_lookup_word("x" * 65) is None


def test_normalize_lookup_text_accepts_complete_phrases() -> None:
    assert normalize_lookup_text("  Laughing   Out Loud! ") == "Laughing Out Loud"
    assert normalize_lookup_text("kick the bucket") == "kick the bucket"
    assert normalize_lookup_text("word") == "word"
    assert normalize_lookup_text(" ".join(["word"] * 25)) is None
    assert selection_lookup_delay_ms("word") == 1000
    assert selection_lookup_delay_ms("Laughing Out Loud") == 2000


def test_parse_dictionary_entry_with_synonyms_and_fallback_phonetic() -> None:
    payload = [
        {
            "word": "luminous",
            "phonetics": [{"text": "/ˈluːmɪnəs/"}],
            "meanings": [
                {
                    "partOfSpeech": "adjective",
                    "synonyms": ["bright", "radiant"],
                    "definitions": [
                        {
                            "definition": "Giving off light; bright or shining.",
                            "example": "a luminous dial",
                            "synonyms": ["radiant", "glowing"],
                        }
                    ],
                }
            ],
        }
    ]
    entry = parse_dictionary_payload(json.dumps(payload))
    assert entry is not None
    assert entry.word == "luminous"
    assert entry.phonetic == "/ˈluːmɪnəs/"
    assert entry.part_of_speech == "adjective"
    assert entry.definition == "Giving off light; bright or shining."
    assert entry.synonyms == ("bright", "radiant", "glowing")
    assert entry.example == "a luminous dial"
    assert entry.source == "DictionaryAPI.dev · online"


def test_parse_dictionary_not_found_and_malformed_payloads() -> None:
    assert parse_dictionary_payload('{"title": "No Definitions Found"}') is None
    assert parse_dictionary_payload("not json") is None
    assert parse_dictionary_payload("[]") is None


def test_dictionary_api_senses_remain_separate() -> None:
    payload = [{
        "word": "spring",
        "meanings": [{
            "partOfSpeech": "verb",
            "definitions": [
                {"definition": "To leap or jump."},
                {"definition": "To arise or come into existence."},
            ],
        }],
    }]
    entries = parse_dictionary_entries(json.dumps(payload))
    assert [entry.definition for entry in entries] == [
        "To leap or jump.",
        "To arise or come into existence.",
    ]


def test_phrase_sources_parse_complete_selection() -> None:
    wiktionary = {
        "en": [{
            "partOfSpeech": "Phrase",
            "definitions": [{"definition": "To <b>laugh audibly</b>."}],
        }]
    }
    entries = parse_wiktionary_entries(json.dumps(wiktionary), "laugh out loud")
    assert entries[0].word == "laugh out loud"
    assert entries[0].definition == "To laugh audibly ."

    wikipedia = {
        "query": {"pages": [{"title": "LOL", "extract": "LOL is an acronym for laughing out loud and indicates amusement."}]}
    }
    context = parse_wikipedia_phrase(json.dumps(wikipedia), "laughing out loud")
    assert context is not None
    assert "complete" not in context.definition
    assert "laughing out loud" in context.definition

    datamuse = [{"word": "lol", "defs": ["v\tTo laugh out loud."]}]
    interpretation = parse_datamuse_phrase(json.dumps(datamuse), "laughing out loud")
    assert interpretation is not None
    assert "Related expression “lol”" in interpretation.definition
    assert interpretation.source == "Datamuse verified phrase relation · online"


def test_datamuse_rejects_unrelated_neighbor_for_complete_phrase() -> None:
    payload = [
        {
            "word": "field",
            "defs": [
                "n\tA land area free of woodland, cities, and towns; an area of open country."
            ],
        },
        {
            "word": "aic",
            "defs": ["n\tAkaike information criterion used for statistical model selection."],
        },
    ]

    assert parse_datamuse_phrase(json.dumps(payload), "addition is commutative") is None


def test_datamuse_skips_unrelated_results_and_uses_phrase_evidence() -> None:
    payload = [
        {"word": "field", "defs": ["n\tAn area of open country."]},
        {
            "word": "commutative addition",
            "defs": ["n\tAddition that is commutative regardless of operand order."],
        },
    ]

    entry = parse_datamuse_phrase(json.dumps(payload), "addition is commutative")

    assert entry is not None
    assert "commutative addition" in entry.definition
    assert "field" not in entry.definition.casefold()


def test_dictionary_cache_persists_successful_results(tmp_path: Path) -> None:
    path = tmp_path / "dictionary-cache.json"
    cache = DictionaryCache(path)
    entry = DictionaryEntry(
        word="luminous",
        phonetic="/ˈluːmɪnəs/",
        part_of_speech="adjective",
        definition="Giving off light.",
        synonyms=("bright", "radiant"),
        source="DictionaryAPI.dev",
    )
    cache.put("LUMINOUS", entry)
    loaded = DictionaryCache(path).get("luminous")
    assert loaded == entry
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_offline_wordnet_handles_inflected_words() -> None:
    entry = lookup_offline_wordnet("torches")
    assert entry is not None
    assert entry.word == "torches"
    assert entry.part_of_speech == "noun"
    assert "light" in entry.definition.casefold()
    assert "offline" in entry.source.casefold()


def test_offline_wordnet_returns_multiple_appendable_senses() -> None:
    entries = lookup_offline_wordnet_entries("sprang", limit=3)
    assert len(entries) >= 2
    assert len({entry.definition for entry in entries}) == len(entries)
    assert all(entry.part_of_speech == "verb" for entry in entries)
