from lumen_reader.models import SearchResult
from lumen_reader.ui import search_results_from_page


def _result(chapter_index: int) -> SearchResult:
    return SearchResult(chapter_index, f"Chapter {chapter_index}", "excerpt", 1)


def test_full_book_search_starts_at_current_page_and_wraps_forward() -> None:
    ordered = search_results_from_page(
        [_result(1), _result(4), _result(7), _result(9)], start_index=6
    )
    assert [result.chapter_index for result in ordered] == [7, 9, 1, 4]


def test_full_book_search_starts_at_current_page_and_wraps_backward() -> None:
    ordered = search_results_from_page(
        [_result(1), _result(4), _result(7), _result(9)], start_index=7, backward=True
    )
    assert [result.chapter_index for result in ordered] == [7, 4, 1, 9]
