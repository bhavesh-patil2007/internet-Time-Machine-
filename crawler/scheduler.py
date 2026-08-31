#	Runs the crawler automatically on a repeating schedule
"""
scheduler.py — Phase 3: run the crawl job across every tracked URL on a
recurring interval, using APScheduler.

WHAT THIS FILE DOES
--------------------
Phases 1 and 2 gave us capture_and_save(url) -- a function that captures
one URL, checks its fidelity, and archives it only if the content changed.
This file is the piece that actually makes the "Internet Time Machine"
automatic: instead of a human running `python crawl.py` by hand every so
often, this script runs forever (until you stop it) and re-crawls every
URL in urls.json on a fixed interval, without any human intervention.

HOW APSCHEDULER FITS IN
--------------------------
APScheduler ("Advanced Python Scheduler") is a library that runs functions
on a schedule -- similar in spirit to a cron job, but living inside a
running Python process instead of being managed by the OS. We use its
`BlockingScheduler`, which is the simplest scheduler type: it takes over
the main thread and runs forever, firing our crawl job every N seconds via
an `IntervalTrigger`. This is the right fit here because this script's
only job IS to run the scheduler -- we don't need it to share the process
with anything else.

WHAT HAPPENS ON EACH SCHEDULED RUN (run_crawl_cycle)
-------------------------------------------------------
1. Load the current urls.json (re-read from disk every cycle, not cached
   at startup -- so if you add a URL to the file while the scheduler is
   already running, e.g. via the future /track API endpoint in Phase 5,
   it gets picked up on the very next cycle without needing a restart).
2. For each URL, call crawl.capture_and_save(url), which:
     - captures the page and runs fidelity checks (Phase 1)
     - hashes the result and skips saving if unchanged (Phase 2)
3. Log a one-line summary per URL: capture status, whether it changed,
   and whether a WARC file was written. This is the "what did the
   scheduler do" audit trail the project spec asks for.
4. Log a cycle-level summary at the end: how many URLs were captured,
   how many were newly saved vs. skipped as unchanged, how many failed.
A single URL raising an unexpected exception does NOT crash the whole
cycle or kill the scheduler -- it's caught, logged, and the loop moves on
to the next URL. A scheduled background job that dies after one bad
capture (e.g. one site is temporarily down) would defeat the whole point
of "runs unattended".

LOGGING
--------
We use Python's built-in `logging` module (rather than bare `print`) so
that every line is automatically timestamped and tagged with a severity
level (INFO / WARNING / ERROR). Logs go to stdout, which is what you want
when running under PM2 (or any process manager) -- PM2 captures stdout
into its own log files, so `pm2 logs scheduler --nostream` shows you
exactly this scheduler's history.

HOW TO RUN IT
---------------
    python scheduler.py                          # every 5 minutes (default)
    python scheduler.py --interval-seconds 60     # every 60 seconds (good for testing)
    python scheduler.py --once                    # run ONE cycle immediately, then exit
                                                    # (does not start the scheduler at all --
                                                    #  useful for testing run_crawl_cycle()
                                                    #  without blocking the terminal forever)
"""

import argparse
import json
import logging
import os
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

import crawl

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("scheduler")

# Quiet down APScheduler's own very chatty per-job-execution INFO logs --
# we log our own summary lines instead, which are more useful here.
logging.getLogger("apscheduler").setLevel(logging.WARNING)

DEFAULT_URLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "urls.json")

# Default recurring interval, in seconds, between crawl cycles. 5 minutes
# is a reasonable default for a small demo project -- frequent enough to
# catch changes in a reasonable time, infrequent enough not to hammer the
# tracked sites. Override with --interval-seconds for testing/demos.
DEFAULT_INTERVAL_SECONDS = 300


def load_urls(urls_file: str = DEFAULT_URLS_FILE) -> list[str]:
    """
    Read the tracked URL list from urls.json.
    Re-read fresh on every call (not cached) so that URLs added to the
    file while the scheduler is already running (e.g. by the future
    POST /track API endpoint in Phase 5) are picked up on the next cycle.
    """
    try:
        with open(urls_file, "r", encoding="utf-8") as f:
            urls = json.load(f)
        if not isinstance(urls, list):
            raise ValueError("urls.json must contain a JSON array of URL strings.")
        return urls
    except FileNotFoundError:
        logger.error(f"URLs file not found: {urls_file}")
        return []
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse {urls_file}: {e}")
        return []


def run_crawl_cycle(urls_file: str = DEFAULT_URLS_FILE) -> dict:
    """
    Run one full crawl pass over every URL in urls.json.

    This is the function APScheduler calls on every tick. It's also
    exposed as a plain function (not just wired into the scheduler) so it
    can be tested directly, or triggered manually via --once, without
    needing a running scheduler at all.

    Returns a summary dict:
        {
            "total": int,
            "saved": int,        # capture succeeded/partial AND content changed -> new WARC written
            "unchanged": int,    # capture succeeded/partial but content was identical to last time
            "failed": int,       # capture_status == "failed" (nothing to hash/save)
            "errors": int,       # an unexpected exception was raised and caught
        }
    """
    urls = load_urls(urls_file)
    logger.info(f"Starting crawl cycle: {len(urls)} tracked URL(s)")

    summary = {"total": len(urls), "saved": 0, "unchanged": 0, "failed": 0, "errors": 0}

    for url in urls:
        try:
            result = crawl.capture_and_save(url)
        except Exception as e:
            # A single misbehaving URL (e.g. a Playwright crash on a
            # pathological page) must not take down the whole scheduler.
            # Log it and keep going with the rest of the list.
            logger.error(f"  {url} -> UNEXPECTED ERROR: {e}")
            summary["errors"] += 1
            continue

        status = result["capture_status"]

        if status == "failed":
            summary["failed"] += 1
            logger.warning(f"  {url} -> FAILED capture ({result['reason']})")
            continue

        # success or partial -- log the fidelity status either way, since
        # "partial" is worth a human's attention even though we still
        # archive it.
        if status == "partial":
            logger.warning(f"  {url} -> PARTIAL capture ({result['reason']})")

        if result["content_changed"]:
            summary["saved"] += 1
            logger.info(
                f"  {url} -> {status.upper()}, CHANGED -> saved "
                f"{os.path.basename(result['warc_path'])}"
            )
        else:
            summary["unchanged"] += 1
            logger.info(f"  {url} -> {status.upper()}, unchanged -> skipped saving")

    logger.info(
        f"Cycle complete: {summary['saved']} saved, {summary['unchanged']} unchanged, "
        f"{summary['failed']} failed, {summary['errors']} errored "
        f"(of {summary['total']} tracked URLs)"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Run the crawl job across all tracked URLs on a recurring interval."
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between crawl cycles (default: {DEFAULT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--urls-file",
        default=DEFAULT_URLS_FILE,
        help="Path to urls.json.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single crawl cycle immediately and exit, without starting "
             "the recurring scheduler. Useful for testing.",
    )
    args = parser.parse_args()

    if args.once:
        run_crawl_cycle(args.urls_file)
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_crawl_cycle,
        trigger=IntervalTrigger(seconds=args.interval_seconds),
        kwargs={"urls_file": args.urls_file},
        # NOTE: deliberately NOT passing next_run_time here. It's tempting
        # to think next_run_time=None means "let the trigger decide", but
        # in APScheduler it actually means "leave this job paused, no
        # next run time at all" -- the job would then NEVER fire on its
        # own. Omitting the argument entirely is what lets add_job()
        # compute the first fire time from the trigger itself (i.e.
        # interval_seconds from now).
        id="crawl_cycle",
        max_instances=1,  # never run two cycles concurrently, even if one runs long
        misfire_grace_time=60,
    )

    # The scheduler's own first fire time (per the trigger, above) is
    # interval_seconds from now, not immediate -- so without this, you'd
    # start the scheduler and see nothing happen for a full interval. We
    # explicitly run one cycle immediately here before entering the
    # blocking loop, so startup behavior is "crawl right away, then every
    # interval_seconds after that".
    logger.info(
        f"Scheduler starting: crawling {len(load_urls(args.urls_file))} tracked URL(s) "
        f"every {args.interval_seconds} seconds. Running first cycle now..."
    )
    run_crawl_cycle(args.urls_file)

    logger.info(f"First cycle done. Scheduler will now run every {args.interval_seconds}s. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
