"""
utils.py — small shared helpers used by multiple crawler modules.

Kept separate (rather than living inside crawl.py) so that warc_writer.py
can use safe_filename_from_url() without creating a circular import
(crawl.py -> warc_writer.py -> crawl.py).
"""

import re
from urllib.parse import urlparse


def safe_filename_from_url(url: str) -> str:
    """
    Turn a URL into a filesystem-safe filename fragment.
    e.g. "https://example.com/foo?x=1" -> "example.com_foo_x1"
    This doesn't need to be reversible, just unique-ish and human-readable.
    """
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}"
    safe = re.sub(r"[^a-zA-Z0-9.\-]+", "_", raw).strip("_")
    return safe or "page"
