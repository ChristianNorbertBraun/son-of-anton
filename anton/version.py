"""The version of this code. A release is a tag `vX.Y.Z` whose anton/version.py says the same."""
import re

__version__ = "0.2.2"
TAG_RE = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def parse(text: str) -> tuple[int, int, int] | None:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    m = TAG_RE.fullmatch(text if text.startswith("v") else f"v{text}")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]
