#Packages captured HTML into proper .warc.gz files
"""
warc_writer.py — Phase 2: package a capture into a WARC file.

WHAT IS A WARC FILE, AND WHY USE ONE?
---------------------------------------
WARC ("Web ARChive") is the standard file format used by real-world web
archives, including the actual Internet Archive / Wayback Machine. A WARC
file is basically a container that holds one or more "records" -- each
record captures one HTTP exchange: the request that was made, and the
response that came back (status line, headers, and body).

We use WARC (via the `warcio` library) instead of just dumping raw HTML to
disk for two reasons:
  1. It's the same format pywb (our replay engine, Phase 4) expects --
     pywb can serve a WARC file's contents back through a browser with
     proper "as of this date" replay semantics, including rewriting
     internal links so the archived page still works.
  2. It keeps HTTP metadata (status code, headers, timestamp) bundled
     together with the page content in one self-describing file, the same
     way a real web archive does it -- rather than us inventing our own
     ad-hoc storage format.

ONE WARC FILE PER CAPTURE
---------------------------
For simplicity (and because our tracked-URL list is small), we write one
small .warc.gz file per successful capture, rather than appending many
captures into a single ever-growing WARC file. This keeps things easy to
reason about: each snapshot = one WARC file, and the database (Phase 5)
just needs to remember which WARC file goes with which snapshot row.
pywb is fine with either approach -- it indexes whatever WARC files exist
in its collection's archive directory, regardless of how they're split up.

WHERE FILES GO
----------------
WARC files are written to crawler/warc/, using a filename built from the
URL and a UTC timestamp, e.g.:
    warc/example.com__20240115T120530Z.warc.gz
This mirrors the naming used for screenshots in crawl.py, so a human
browsing the two folders can visually match a WARC file to its screenshot.
"""

import io
import os
from datetime import datetime, timezone

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from utils import safe_filename_from_url

DEFAULT_WARC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warc")


def write_warc(capture: dict, warc_dir: str = DEFAULT_WARC_DIR) -> str:
    """
    Write a single capture result (as returned by crawl.capture_page) out
    as a one-record WARC file, and return the path to the file written.

    We write a "response" record -- the WARC record type that represents
    "here is what the server sent back for this URL" -- containing:
      - WARC-Target-URI: the URL that was captured
      - WARC-Date: when it was captured
      - an embedded HTTP response (status line + Content-Type header +
        the rendered HTML body)

    Parameters
    ----------
    capture : dict
        A result dict as produced by crawl.capture_page(), i.e. it must
        have at least: url, html, http_status, captured_at.
    warc_dir : str
        Directory to write the .warc.gz file into (created if missing).

    Returns
    -------
    str
        Absolute path to the WARC file that was written.
    """
    if not capture.get("html"):
        raise ValueError("Cannot write a WARC record: capture has no HTML content.")

    os.makedirs(warc_dir, exist_ok=True)

    filename_base = safe_filename_from_url(capture["url"])
    # Use the capture's own timestamp (not "now") so the WARC filename
    # reflects when the page was actually captured, not when this function
    # happened to run.
    captured_at = capture.get("captured_at") or datetime.now(timezone.utc).isoformat()
    # captured_at is an ISO string like "2024-01-15T12:05:30.123456+00:00" --
    # turn it into a filesystem-friendly tag. We include milliseconds
    # (not just whole seconds) because two captures of the SAME URL can
    # legitimately happen within the same second (e.g. back-to-back manual
    # test runs, or a very short scheduler interval) -- without sub-second
    # precision, the second write would silently overwrite the first one
    # on disk since they'd produce an identical filename.
    dt = datetime.fromisoformat(captured_at)
    timestamp_tag = dt.strftime("%Y%m%dT%H%M%S") + f"{dt.microsecond // 1000:03d}Z"

    warc_filename = f"{filename_base}__{timestamp_tag}.warc.gz"
    warc_path = os.path.join(warc_dir, warc_filename)

    html_bytes = capture["html"].encode("utf-8")
    http_status = capture.get("http_status") or 200

    with open(warc_path, "wb") as out:
        writer = WARCWriter(out, gzip=True)

        # StatusAndHeaders represents the HTTP response line + headers that
        # get embedded inside the WARC record, e.g.:
        #   HTTP/1.1 200 OK
        #   Content-Type: text/html; charset=UTF-8
        http_headers = StatusAndHeaders(
            f"{http_status} OK" if http_status == 200 else f"{http_status} Non-200",
            [("Content-Type", "text/html; charset=UTF-8")],
            protocol="HTTP/1.1",
        )

        record = writer.create_warc_record(
            capture["url"],
            "response",
            payload=io.BytesIO(html_bytes),
            http_headers=http_headers,
        )

        # Overwrite the auto-generated WARC-Date with the capture's actual
        # timestamp, so the archive's notion of "when was this captured"
        # matches when Playwright actually grabbed the page, not when we
        # happened to write the WARC file to disk.
        record.rec_headers.replace_header("WARC-Date", dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

        writer.write_record(record)

    return os.path.abspath(warc_path)


def read_warc_summary(warc_path: str) -> list[dict]:
    """
    Read back a WARC file and return a summary of the response record(s)
    it contains. Useful for manual verification / debugging -- lets us
    confirm "yes, this WARC file really does contain what we think it
    does" without needing pywb running.
    """
    from warcio.archiveiterator import ArchiveIterator

    summaries = []
    with open(warc_path, "rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                content = record.content_stream().read()
                summaries.append({
                    "target_uri": record.rec_headers.get_header("WARC-Target-URI"),
                    "warc_date": record.rec_headers.get_header("WARC-Date"),
                    "content_length": len(content),
                    "content_preview": content[:200].decode("utf-8", errors="replace"),
                })
    return summaries


if __name__ == "__main__":
    # Manual smoke test: build a fake "capture" dict and write/read it back.
    fake_capture = {
        "url": "https://example.com",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "html": "<html><head><title>Test</title></head><body><h1>Hello WARC</h1></body></html>",
        "http_status": 200,
    }
    path = write_warc(fake_capture, warc_dir="/tmp/warc_smoke_test")
    print(f"Wrote: {path}")

    for summary in read_warc_summary(path):
        print(summary)
