#	Change detection — hashes HTML, compares to last version
"""
hash_check.py — Phase 2: SHA-256 based change detection.

WHY WE NEED THIS
-----------------
Re-rendering and saving a full WARC file for a page that hasn't actually
changed since last time is wasted disk space and wasted crawl time. So
before we decide to save a new snapshot, we want to answer: "does this
capture look meaningfully different from the last one we saved for this
URL?"

We answer that by hashing the page's HTML with SHA-256 and comparing it to
the hash we stored last time. If the hashes match, nothing changed (as far
as we can tell) -- skip saving. If they differ (or this is the first time
we've ever seen this URL), it's new content -- go ahead and save it.

WHY WE "CLEAN" THE HTML FIRST
-------------------------------
Lots of pages embed things that change on every single load even when the
*content* a human cares about hasn't changed at all:
  - CSRF tokens: <input name="csrf_token" value="a1b2c3...">
  - Cache-busting query strings in inline <script src="app.js?v=1699999999">
  - Ad server iframes with unique per-request IDs
  - Timestamps rendered into the page ("Last updated 2 seconds ago")

If we hashed the raw HTML as-is, the hash would change on almost every
crawl even when nothing meaningful changed, defeating the whole point of
change detection. So before hashing, we strip out the most common sources
of this "noise": <script> and <style> tag *contents* (but we keep the tags
themselves, since removing a whole tag could itself signal a real change),
HTML comments, and long sequences of whitespace.

This is intentionally a simple, transparent heuristic (regex-based, not a
full HTML parser) -- good enough for a small tracked-URL list, and easy to
explain/extend for a course project. It is NOT perfect: a page that embeds
a changing value directly in visible text (e.g. "1234 people online now")
will still register as "changed" every time. That's a reasonable, honest
limitation to call out in your write-up.

WHERE THE LAST-KNOWN HASHES LIVE
----------------------------------
last_hashes.json is a simple JSON file mapping url -> last saved hash:
    {
        "https://example.com": "a1b2c3...",
        "https://news.ycombinator.com": "d4e5f6..."
    }
It's intentionally just a flat file (not the database) because at this
phase we don't have Supabase wired up yet (that's Phase 5) -- and honestly,
"what was the last hash for this URL" is really the crawler's own local
bookkeeping, so a small JSON file is a fine place for it even later.
"""

import hashlib
import json
import os
import re

# Path to the JSON file that stores {url: last_saved_hash}.
LAST_HASHES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_hashes.json")


def clean_html(html: str) -> str:
    """
    Strip out common sources of "noise" that change on every page load
    without reflecting a real content change, so hashing is more stable.

    Removes:
      - contents of <script>...</script> tags (keeps the tags)
      - contents of <style>...</style> tags (keeps the tags)
      - HTML comments <!-- ... -->
    Then collapses all runs of whitespace down to a single space, and
    strips leading/trailing whitespace, so purely cosmetic re-indentation
    or blank-line differences don't register as changes either.
    """
    if not html:
        return ""

    # re.DOTALL makes '.' match newlines too, so multi-line script/style
    # blocks get matched as a single block.
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", "<script></script>", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", "<style></style>", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)

    # Collapse whitespace so indentation-only differences don't matter.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def compute_hash(html: str) -> str:
    """
    Compute the SHA-256 hash (as a hex string) of the *cleaned* HTML.
    This is the value we compare across crawls to detect real changes.
    """
    cleaned = clean_html(html)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _load_last_hashes(path: str = LAST_HASHES_PATH) -> dict:
    """Load the {url: hash} map from disk. Returns {} if the file doesn't exist yet."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # Corrupt/empty file -- treat as "no history" rather than crashing.
            return {}


def _save_last_hashes(hashes: dict, path: str = LAST_HASHES_PATH) -> None:
    """Persist the {url: hash} map to disk as pretty-printed JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2, sort_keys=True)


def has_content_changed(url: str, html: str, path: str = LAST_HASHES_PATH) -> tuple[bool, str, str | None]:
    """
    Compare this capture's hash against the last known hash for this URL.

    Returns a tuple: (changed, new_hash, previous_hash)
      - changed:       True if this is the first-ever capture of this URL,
                        or if the content hash differs from last time.
      - new_hash:       the SHA-256 hash of this capture's cleaned HTML.
      - previous_hash:  the hash we had stored before this call, or None if
                        this URL has never been seen before.

    NOTE: this function does NOT update last_hashes.json. It's read-only by
    design -- the caller decides whether to actually save the new snapshot
    (e.g. a "failed" capture shouldn't overwrite a good previous hash), and
    only calls record_new_hash() once it has done so.
    """
    new_hash = compute_hash(html)
    known_hashes = _load_last_hashes(path)
    previous_hash = known_hashes.get(url)

    changed = previous_hash is None or previous_hash != new_hash
    return changed, new_hash, previous_hash


def record_new_hash(url: str, new_hash: str, path: str = LAST_HASHES_PATH) -> None:
    """
    Persist `new_hash` as the latest known hash for `url` in last_hashes.json.
    Call this only after you've actually saved the corresponding snapshot
    (e.g. written a WARC record for it) -- otherwise the bookkeeping and the
    actual saved data can drift out of sync.
    """
    known_hashes = _load_last_hashes(path)
    known_hashes[url] = new_hash
    _save_last_hashes(known_hashes, path)


if __name__ == "__main__":
    # Small manual smoke test.
    #
    # html_a / html_b differ ONLY in things the cleaner is designed to
    # strip: a cache-busting query string on a <script src>, an inline
    # <script> BODY (e.g. an analytics beacon with a random session id),
    # and an HTML comment with a timestamp. Real visible content (the
    # <h1>) is identical -> these should hash the SAME.
    #
    # html_c changes the actual visible content (<h1> text) -> this should
    # hash DIFFERENTLY from html_a.
    #
    # NOTE: a changing attribute value on a normal tag (e.g. a per-request
    # CSRF token on an <input>) is NOT stripped by this cleaner -- only
    # script/style tag bodies and comments are. That's a known, documented
    # limitation (see the module docstring above), not tested here.
    html_a = """
    <html><head><script src="app.js?v=1699999999"></script></head>
    <body>
      <!-- rendered 2024-01-01T00:00:00Z -->
      <script>var sessionId = "abc123"; trackPageview();</script>
      <h1>Hello World</h1>
    </body></html>
    """
    html_b = """
    <html><head><script src="app.js?v=1700000042"></script></head>
    <body>
      <!-- rendered 2024-01-01T00:00:05Z -->
      <script>var sessionId = "xyz789"; trackPageview();</script>
      <h1>Hello World</h1>
    </body></html>
    """
    html_c = """
    <html><head><script src="app.js?v=1700000042"></script></head>
    <body>
      <h1>Hello Completely Different World</h1>
    </body></html>
    """

    print("hash(a) == hash(b) (should be True, only noise differs):",
          compute_hash(html_a) == compute_hash(html_b))
    print("hash(a) == hash(c) (should be False, real content differs):",
          compute_hash(html_a) == compute_hash(html_c))
