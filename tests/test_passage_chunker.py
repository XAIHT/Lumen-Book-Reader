from __future__ import annotations

from lumen_reader.passage_chunker import ChunkerConfig, PassageChunker


def test_passage_chunker_is_deterministic_bounded_and_offset_exact() -> None:
    text = " ".join(f"word{index}." for index in range(900))
    chunker = PassageChunker(ChunkerConfig(
        target_tokens=80,
        soft_min_tokens=50,
        soft_max_tokens=100,
        hard_max_tokens=120,
        overlap_tokens=10,
    ))
    first = list(chunker.chunks(text))
    second = list(chunker.chunks(text))
    assert first == second
    assert len(first) > 2
    for index, chunk in enumerate(first):
        assert text[chunk.char_start:chunk.char_end] == chunk.text
        assert len(chunk.text) <= 120 * 4
        assert chunk.ordinal == index
        assert len(chunk.content_sha256) == 64


def test_passage_chunker_ignores_empty_unicode_safely() -> None:
    assert list(PassageChunker().chunks("\ud800\n  \x00")) == []
