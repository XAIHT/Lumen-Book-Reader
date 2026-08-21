"""Lumen EPUB and PDF Reader."""

from __future__ import annotations

# The version is resolved lazily (PEP 562) so importing the package never pays
# for the `git describe` fallback unless something actually asks. Git tags are
# the source of truth; see lumen_reader/version.py and RELEASING.md.
__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    if name == "__version__":
        from .version import get_version

        return get_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"__version__"})
