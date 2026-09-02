"""Deterministic Reciprocal Rank Fusion for future semantic candidates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for position, item_id in enumerate(ranking, start=1):
            scores[int(item_id)] += 1.0 / (max(1, int(k)) + position)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
