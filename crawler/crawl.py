#Python packages needed (already done ✅)
"""
crawl.py — Playwright-based page capture, with fidelity checks (Phase 1)
and change-detection-gated WARC saving (Phase 2).

WHAT THIS FILE DOES
--------------------
Given a URL, this module launches a real (headless) Chromium browser,
navigates to the page, waits for JavaScript to finish rendering, and then
pulls out:
  - the fully-rendered HTML (after JS has run — this is the key advantage
    over a simple `requests.get()`, which only gets the raw server response
    before any client-side JavaScript executes)
  - the HTTP status code of the response
  - the page title
  - a screenshot (saved to disk, for humans to eyeball later)

It then runs "fidelity checks" — sanity checks that answer "did this capture
actually work, or did we just save a blank/broken page?" — and labels the
result as one of:
  - "success": everything looks fine
  - "partial": we got *something*, but one of the sanity checks failed
               (e.g. suspiciously short content, or no <title>)
  - "failed":  we didn't get a usable page at all (network error, timeout,
               or a clear HTTP error status)

PHASE 2 ADDITION: change-detection-gated saving
--------------------------------------------------
capture_page() alone just captures -- it doesn't decide whether to keep
the result. capture_and_save() (below) wraps it with the Phase 2 pipeline:
  1. Capture the page (as above).
  2. If we got HTML at all, hash it (via hash_check) and compare against
     the last hash we saved for this URL.
  3. If the content is unchanged since last time, skip saving a WARC file
     -- there's nothing new to archive, so don't waste disk space.
  4. If the content changed (or this is the first-ever capture of this
     URL), write a WARC file (via warc_writer) and remember the new hash
     for next time.
Note the fidelity status (success/partial/failed) and the save decision
are independent: even a "partial" capture still gets checked for changes
and saved if its content differs -- the fidelity label is just metadata
describing how much we trust the capture, not a filter on whether to
archive it. A "failed" capture with no HTML at all obviously has nothing
to hash or save, so it's always skipped.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import hash_check
import warc_writer
from utils import safe_filename_from_url

# Phase 5: the API's db.py module lives in ../api/. We add that directory
# to sys.path so the crawler can import it directly and write a row into
# Postgres after successfully archiving a page -- without duplicating any
# DB code here. If api/db.py (or its DATABASE_URL) isn't set up yet, the
# import/insert is wrapped in a try/except below so the crawler still
# works standalone with zero DB configured (see record_snapshot_in_db()).
_API_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

# Phase 4: path to the helper script that hands a newly-written WARC file
# off to pywb (which lives in its own separate Python 3.11 virtualenv --
# see pywb-data/add_to_collection.sh for why). Registering with pywb is
# what makes a capture actually replayable through the timeline UI later.
PYWB_ADD_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "pywb-data", "add_to_collection.sh"
)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# How long (in milliseconds) we'll wait for a page to finish navigating
# before giving up and calling it a failure.
NAV_TIMEOUT_MS = 30_000

# After the main navigation finishes, we also wait for the network to go
# "idle" (no requests in flight for 500ms) for up to this long. This gives
# JS-heavy single-page apps time to fetch their data and render it. If this
# times out we don't treat it as fatal -- we just move on with whatever HTML
# exists at that point, since some sites (e.g. ones with live polling/ads)
# never truly go idle.
NETWORK_IDLE_TIMEOUT_MS = 8_000

# If the captured HTML is shorter than this many characters, we consider it
# "suspiciously short" -- likely a blank page, an error page, or a login
# wall, rather than real content.
MIN_CONTENT_LENGTH = 500

# Where screenshots get saved (relative to this file's directory unless an
# absolute path is passed in).
DEFAULT_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


def capture_page(url: str, screenshot_dir: str = DEFAULT_SCREENSHOT_DIR) -> dict:
    """
    Capture a single URL with a headless browser and run fidelity checks.

    Returns a dict shaped like:
    {
        "url": str,
        "captured_at": ISO-8601 UTC timestamp string,
        "html": str | None,          # full rendered HTML, or None if capture totally failed
        "title": str | None,
        "http_status": int | None,   # None if we never got a response (e.g. DNS failure)
        "content_length": int,       # len(html) if we have it, else 0
        "screenshot_path": str | None,
        "capture_status": "success" | "partial" | "failed",
        "reason": str | None,        # human-readable explanation when not "success"
    }
    """
    result = {
        "url": url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "html": None,
        "title": None,
        "http_status": None,
        "content_length": 0,
        "screenshot_path": None,
        "capture_status": "failed",
        "reason": None,
    }

    os.makedirs(screenshot_dir, exist_ok=True)
    filename_base = safe_filename_from_url(url)
    # Include milliseconds, not just whole seconds: two captures of the
    # same URL can happen within the same second (e.g. back-to-back test
    # runs), and without sub-second precision the second screenshot would
    # silently overwrite the first one on disk.
    _now = datetime.now(timezone.utc)
    timestamp_tag = _now.strftime("%Y%m%dT%H%M%S") + f"{_now.microsecond // 1000:03d}Z"
    screenshot_path = os.path.join(screenshot_dir, f"{filename_base}__{timestamp_tag}.png")

    with sync_playwright() as p:
        # headless=True means no visible browser window -- this is what
        # lets the crawler run on a server with no display attached.
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            # --- Step 1: Navigate ---
            # page.goto() returns the Response object for the main document
            # request. wait_until="domcontentloaded" means "wait until the
            # HTML has been parsed", which is earlier than full page load --
            # we deliberately don't wait for ALL resources (images, etc.)
            # here because that can hang forever on slow-loading assets;
            # instead we do a separate, bounded wait for network idle below.
            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                result["capture_status"] = "failed"
                result["reason"] = f"Navigation timed out after {NAV_TIMEOUT_MS}ms"
                return result
            except Exception as e:
                # Covers DNS failures, connection refused, SSL errors, etc.
                result["capture_status"] = "failed"
                result["reason"] = f"Navigation error: {e}"
                return result

            if response is None:
                # This can happen for things like about:blank or some
                # redirects Playwright doesn't attach a response to.
                result["capture_status"] = "failed"
                result["reason"] = "No HTTP response object returned by browser"
                return result

            result["http_status"] = response.status

            # --- Step 2: Give JS-heavy pages a chance to finish rendering ---
            # We don't treat a timeout here as fatal -- plenty of real
            # sites (ones with polling, ads, analytics beacons) never go
            # fully idle. We just capture whatever is on the page after
            # waiting a bounded amount of time.
            try:
                page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass  # Not fatal -- proceed with whatever has rendered so far.

            # --- Step 3: Pull out the rendered HTML, title, screenshot ---
            html = page.content()
            title = page.title()

            try:
                page.screenshot(path=screenshot_path, full_page=True)
                result["screenshot_path"] = screenshot_path
            except Exception as e:
                # A failed screenshot shouldn't sink the whole capture --
                # we still have the HTML, which is the primary artifact.
                result["screenshot_path"] = None
                print(f"  [warn] screenshot failed for {url}: {e}", file=sys.stderr)

            result["html"] = html
            result["title"] = title
            result["content_length"] = len(html) if html else 0

        finally:
            browser.close()

    # --- Step 4: Fidelity checks -> decide success / partial / failed ---
    result["capture_status"], result["reason"] = _classify_capture(result)
    return result


def _classify_capture(result: dict) -> tuple[str, str | None]:
    """
    Apply the three fidelity checks described in the project spec:
      1. HTTP status must be 200
      2. Content length must not be suspiciously short
      3. Page must have a non-empty title

    Returns (capture_status, reason).
    - Any check failing outright (bad HTTP status, or no HTML at all) => "failed"
    - HTTP status OK but content-length or title checks fail          => "partial"
    - All checks pass                                                  => "success"
    """
    http_status = result["http_status"]
    content_length = result["content_length"]
    title = result["title"]

    # A clear server-side error (4xx/5xx) or a missing status means the
    # capture is not trustworthy at all.
    if http_status is None or http_status >= 400:
        return "failed", f"Unexpected HTTP status: {http_status}"

    problems = []
    if content_length < MIN_CONTENT_LENGTH:
        problems.append(
            f"content length {content_length} is below minimum {MIN_CONTENT_LENGTH}"
        )
    if not title or not title.strip():
        problems.append("page title is empty")

    if problems:
        return "partial", "; ".join(problems)

    return "success", None


def capture_and_save(
    url: str,
    screenshot_dir: str = DEFAULT_SCREENSHOT_DIR,
    warc_dir: str = warc_writer.DEFAULT_WARC_DIR,
    hashes_path: str = hash_check.LAST_HASHES_PATH,
) -> dict:
    """
    Phase 2 entry point: capture a URL, then decide whether it's worth
    saving a new snapshot for, based on whether the content actually
    changed since the last time we saved this URL.

    This wraps capture_page() (Phase 1) and adds four fields to the
    result dict it returns:
      - "content_hash":  SHA-256 hash of this capture's cleaned HTML,
                          or None if there was no HTML to hash (failed capture).
      - "content_changed": True/False/None (None = nothing to compare, i.e.
                          failed capture with no HTML).
      - "warc_path":     path to the WARC file written for this capture,
                          or None if we skipped saving (unchanged content,
                          or no HTML to save in the first place).
      - "pywb_registered": True if a new WARC was written AND successfully
                          handed off to pywb for replay (Phase 4). False if
                          nothing was saved, or if saving succeeded but the
                          pywb handoff failed/was skipped (the WARC file on
                          disk is unaffected either way -- see
                          register_with_pywb()).

    Decision logic:
      - No HTML at all (capture_status == "failed" with html=None) ->
        nothing to hash, nothing to save. content_hash/content_changed/
        warc_path all stay None.
      - HTML exists, but its hash matches the last saved hash for this URL
        -> content unchanged, skip writing a WARC file. We still return
        content_hash and content_changed=False so callers can see what
        happened.
      - HTML exists and the hash differs from last time (or this is the
        first-ever capture of this URL) -> write a WARC file, then record
        the new hash so future runs compare against it.
    """
    result = capture_page(url, screenshot_dir=screenshot_dir)
    result["content_hash"] = None
    result["content_changed"] = None
    result["warc_path"] = None
    result["pywb_registered"] = False
    result["db_row_id"] = None

    if not result["html"]:
        # Nothing was captured (failed navigation) -- there's nothing to
        # hash or save. Leave the change-detection fields as None so the
        # caller can distinguish "we never even checked" from "we checked
        # and it was unchanged".
        return result

    changed, new_hash, _previous_hash = hash_check.has_content_changed(
        url, result["html"], path=hashes_path
    )
    result["content_hash"] = new_hash
    result["content_changed"] = changed

    if not changed:
        # Content is identical to what we already have archived -- skip
        # writing a new WARC file to save disk space.
        return result

    # Content is new or changed -- archive it, then remember this hash so
    # the next run can compare against it.
    warc_path = warc_writer.write_warc(result, warc_dir=warc_dir)
    result["warc_path"] = warc_path
    hash_check.record_new_hash(url, new_hash, path=hashes_path)

    # Phase 5: record this snapshot's metadata in Postgres. Best-effort,
    # same reasoning as pywb registration below -- a DB hiccup (e.g. no
    # DATABASE_URL configured yet) must not undo the fact that the WARC
    # file is already safely saved to disk.
    result["db_row_id"] = record_snapshot_in_db(result)

    # Phase 4: register the new WARC with pywb so it becomes replayable
    # right away. This is a best-effort step -- a pywb registration
    # failure (e.g. pywb isn't set up yet, as would be true before Phase 4
    # exists) should NOT undo the fact that we already successfully
    # archived the page; the WARC file on disk is the source of truth, and
    # it can always be re-added to pywb later via wb-manager if this step
    # is skipped or fails.
    result["pywb_registered"] = register_with_pywb(warc_path)

    return result


def record_snapshot_in_db(result: dict):
    """
    Insert this capture's metadata into the Postgres `snapshots` table
    (Phase 5), via api/db.py's insert_snapshot(). Returns the new row's
    id, or None if the insert was skipped/failed (e.g. api/.env has no
    DATABASE_URL yet, or the DB is unreachable) -- logged to stderr but
    never raised, matching register_with_pywb()'s best-effort pattern.
    """
    try:
        import db as _db  # imported lazily so a missing/misconfigured
        # api/db.py doesn't break `import crawl` for anyone not using the DB.

        return _db.insert_snapshot(
            url=result["url"],
            captured_at=datetime.fromisoformat(result["captured_at"]),
            content_hash=result["content_hash"],
            warc_path=result["warc_path"],
            http_status=result["http_status"],
            capture_status=result["capture_status"],
        )
    except Exception as e:
        print(f"  [warn] DB insert failed for {result['url']}: {e}", file=sys.stderr)
        return None


def register_with_pywb(warc_path: str) -> bool:
    """
    Hand a WARC file off to pywb's collection so it becomes replayable.

    Returns True if registration succeeded, False otherwise (including if
    the pywb helper script doesn't exist yet -- e.g. if this function is
    called before Phase 4's pywb collection has been set up). Failures are
    logged to stderr but never raised, since a pywb hiccup shouldn't be
    treated as a capture failure -- the WARC file itself is already safely
    saved to disk regardless of what happens here.
    """
    if not os.path.exists(PYWB_ADD_SCRIPT):
        print(
            f"  [info] pywb helper script not found at {PYWB_ADD_SCRIPT} -- "
            f"skipping pywb registration (has Phase 4 setup been run?)",
            file=sys.stderr,
        )
        return False

    try:
        subprocess.run(
            [PYWB_ADD_SCRIPT, warc_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [warn] pywb registration failed for {warc_path}: {e.stderr}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  [warn] pywb registration timed out for {warc_path}", file=sys.stderr)
        return False


def _print_summary(result: dict) -> None:
    """Pretty-print a capture result to the terminal for manual testing."""
    print(f"URL:              {result['url']}")
    print(f"Captured at:      {result['captured_at']}")
    print(f"HTTP status:      {result['http_status']}")
    print(f"Title:            {result['title']!r}")
    print(f"Content length:   {result['content_length']} chars")
    print(f"Screenshot:       {result['screenshot_path']}")
    print(f"Capture status:   {result['capture_status'].upper()}")
    if result["reason"]:
        print(f"Reason:           {result['reason']}")

    # Phase 2 fields -- only present when capture_and_save() was used
    # (main() below always uses it, but capture_page() alone doesn't add
    # these keys, so we check before printing).
    if "content_changed" in result:
        if result["content_changed"] is None:
            print("Change detection: N/A (no HTML captured)")
        elif result["content_changed"]:
            print(f"Change detection: CHANGED -> saved new WARC")
            print(f"WARC file:        {result['warc_path']}")
            print(f"DB row id:        {result.get('db_row_id')}")
            print(f"pywb registered:  {result.get('pywb_registered', False)}")
        else:
            print(f"Change detection: UNCHANGED -> skipped saving (identical to last snapshot)")
        print(f"Content hash:     {result['content_hash']}")

    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Capture one or more URLs with Playwright, run fidelity checks, "
                    "and save a WARC snapshot only if the content changed since last time."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URL(s) to capture. If omitted, reads all URLs from urls.json.",
    )
    parser.add_argument(
        "--urls-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "urls.json"),
        help="Path to urls.json (used when no URLs are given on the command line).",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Only run Phase 1 capture + fidelity checks; skip Phase 2 change "
             "detection and WARC saving entirely.",
    )
    args = parser.parse_args()

    urls = args.urls
    if not urls:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls = json.load(f)

    for url in urls:
        print(f"Capturing: {url}")
        if args.no_save:
            result = capture_page(url)
        else:
            result = capture_and_save(url)
        _print_summary(result)


if __name__ == "__main__":
    main()
