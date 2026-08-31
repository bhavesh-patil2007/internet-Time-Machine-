# Project docs, synopsis, and paper drafts go here
# Internet Time Machine

A lightweight, self-hosted web archiving and time-travel playback system —
similar in spirit to the Wayback Machine, but scoped to a small, user-chosen
list of tracked URLs.

## Status: Phase 7 of 7 complete — all phases built and verified end-to-end

This project is being built incrementally. See the Roadmap below for what's
done and what's next.

## Project Overview

- **Goal**: Periodically capture full renders (including JS) of a small list
  of tracked websites, detect when content actually changed, store snapshots
  as WARC files, and let a user browse/replay any URL "as of" a chosen date
  via a timeline UI.
- **Team**: 2 (college project)

## Architecture

| Layer | Technology |
|---|---|
| Crawler | Python + Playwright (headless Chromium) |
| Change detection | SHA-256 hash of rendered HTML |
| Archive format | WARC (via `warcio`) |
| Scheduler | APScheduler |
| Storage + playback | pywb (Memento-style replay) |
| Metadata DB | PostgreSQL via Supabase |
| API | FastAPI |
| Frontend | React (Vite) + Tailwind CSS |

## Directory Structure

```
webapp/
├── crawler/            # Python + Playwright capture pipeline
│   ├── crawl.py        # Page capture + fidelity checks + save orchestration (Phase 1+2 ✅)
│   ├── hash_check.py   # SHA-256 change detection (Phase 2 ✅)
│   ├── warc_writer.py  # WARC packaging (Phase 2 ✅)
│   ├── utils.py         # Shared helpers (URL -> safe filename)
│   ├── scheduler.py    # Recurring crawl scheduling (Phase 3 ✅)
│   ├── urls.json       # Tracked URL list
│   ├── screenshots/    # Per-capture screenshots (gitignored, .gitkeep only)
│   ├── warc/           # Per-snapshot WARC files (gitignored, .gitkeep only)
│   ├── last_hashes.json # {url: last saved content hash} (gitignored, regenerated)
│   ├── venv/           # Python virtual environment (gitignored)
│   └── requirements.txt
├── api/                # FastAPI backend (Phase 5)
│   ├── main.py         # FastAPI app: /snapshots, /snapshot/closest, /tracked-urls, /track
│   ├── db.py           # Postgres/Supabase connection + queries
│   ├── models.py       # Pydantic request/response schemas
│   ├── .env.example    # DATABASE_URL template
│   ├── venv/           # Python 3.13 virtual environment (gitignored)
│   └── requirements.txt
├── frontend/           # React + Vite + Tailwind UI (Phase 6)
│   ├── src/
│   │   ├── api.js          # The only file that calls the FastAPI backend
│   │   ├── pages/          # Home.jsx, Timeline.jsx, Compare.jsx
│   │   ├── components/     # Layout, SearchBar, TimelineSlider, SnapshotViewer, etc.
│   │   └── hooks/useDarkMode.js
│   └── .env.example    # VITE_API_BASE / VITE_PYWB_BASE template
├── pywb-data/          # pywb WARC collection storage (Phase 4)
│   ├── add_to_collection.sh  # cross-venv bridge: wb-manager add <warc>
│   ├── collections/webapp-collection/  # archive/ + indexes/ (gitignored, regenerated)
│   └── venv/           # Python 3.11 virtual environment (gitignored)
├── docs/
│   └── schema.sql      # Postgres `snapshots` table definition
├── ecosystem.config.cjs  # PM2: runs api + pywb + frontend together
└── README.md
```

## Phase 1 — Crawler Foundation (✅ Done)

**What was built:**
- `crawler/urls.json` — 3 test URLs: a static site (`example.com`), a
  JS-heavy SPA-style site (`quotes.toscrape.com/js/`), and a real-world news
  aggregator (`news.ycombinator.com`).
- `crawler/crawl.py` — `capture_page(url)`:
  1. Launches headless Chromium via Playwright.
  2. Navigates to the URL, waits for the DOM to parse, then waits (with a
     bounded timeout) for network activity to go idle so client-side
     JavaScript has a chance to render dynamic content.
  3. Extracts the fully-rendered HTML, `<title>`, and HTTP status.
  4. Saves a full-page screenshot to `crawler/screenshots/`.
  5. Runs **fidelity checks** and classifies the result:
     - `success` — HTTP 200, content length ≥ 500 chars, non-empty title.
     - `partial` — HTTP 200 but content suspiciously short and/or no title.
     - `failed` — bad/missing HTTP status, navigation error, or timeout.

**How to run it manually:**
```bash
cd crawler
source venv/bin/activate
python crawl.py                                   # captures every URL in urls.json
python crawl.py "https://example.com"              # capture just one URL
```

**Verified during testing:**
- Static site (`example.com`) → `success`, real title + content.
- JS-heavy site (`quotes.toscrape.com/js/`) → `success`, and the rendered
  HTML was confirmed to contain content injected by client-side JS (proving
  Playwright's JS rendering, not just the raw server response, was captured).
- Failure classification verified with synthetic cases (404 status, DNS
  failure, short content, empty title) — each correctly classified as
  `failed` or `partial`.

**Known environment note:** this sandbox runs Python 3.13. Playwright
1.47-1.49 pin an exact old `greenlet`/`pyee` version that has no prebuilt
wheel for 3.13, forcing a from-source build that fails. `requirements.txt`
pins Playwright `1.50.0` instead, the first release that relaxed those
pins to flexible ranges — this lets a single `pip install -r
requirements.txt` resolve cleanly with no manual workaround needed (see
the comment in that file for details; this was verified with a from-
scratch venv rebuild during Phase 3 testing, see below).

## Phase 2 — Change Detection + WARC Storage (✅ Done)

**What was built:**
- `crawler/hash_check.py`:
  - `clean_html(html)` — strips the contents of `<script>`/`<style>` tags
    and HTML comments, and collapses whitespace, before hashing. This
    avoids false "changed" detections caused by cache-busting query
    strings, embedded timestamps, or per-request analytics snippets that
    have nothing to do with the page's actual visible content.
  - `compute_hash(html)` — SHA-256 hex digest of the cleaned HTML.
  - `has_content_changed(url, html)` — compares the new hash against the
    last known hash for that URL (read from `last_hashes.json`). Returns
    `changed=True` on the very first capture of a URL, or whenever the
    hash differs from last time.
  - `record_new_hash(url, hash)` — persists the new hash to
    `last_hashes.json`, but only called *after* a snapshot is actually
    saved, so bookkeeping never drifts out of sync with what's on disk.
- `crawler/warc_writer.py`:
  - `write_warc(capture)` — packages one capture's HTML + HTTP status +
    timestamp into a one-record `.warc.gz` file (the same format pywb
    will replay in Phase 4), named
    `<url-slug>__<timestamp-with-ms>.warc.gz`.
  - `read_warc_summary(path)` — reads a WARC file back for verification
    (used in manual testing, not part of the main pipeline).
- `crawler/crawl.py`: added `capture_and_save(url)`, which wraps Phase 1's
  `capture_page()` with the Phase 2 pipeline — hash the result, skip
  saving if unchanged, otherwise write a WARC file and record the new
  hash. This is now what `crawl.py`'s CLI uses by default (pass `--no-save`
  to fall back to Phase-1-only behavior).

**How to run it manually:**
```bash
cd crawler
source venv/bin/activate
python crawl.py                 # capture + save (skips unchanged URLs)
python crawl.py --no-save       # Phase 1 only: capture + fidelity check, no saving
```

**Verified during testing:**
- Ran the crawler twice in a row on the same URLs: **first run** detected
  each URL as new (no prior hash on record) and wrote a WARC file for
  each; **second run** (content unchanged) correctly reported
  `UNCHANGED -> skipped saving` for every URL, and confirmed via `ls` that
  no new WARC files were written — the WARC directory still contained
  exactly the files from the first run.
- Verified WARC files are valid and byte-correct by reading them back with
  `warcio.ArchiveIterator` and confirming the target URI, timestamp, and
  full HTML content all round-trip correctly.
- Verified the cleaning logic with a synthetic test: two HTML snippets
  that differ only in a cache-busting script query string, an inline
  analytics snippet, and an HTML comment timestamp hash **identically**
  after cleaning; a snippet with genuinely different visible text hashes
  **differently**.
- Found and fixed a real bug during testing: WARC/screenshot filenames
  only had whole-second timestamp precision, so two captures of the same
  URL within the same second would silently overwrite each other's file.
  Fixed by adding millisecond precision to both filename schemes.
- **Known limitation** (by design, documented in `hash_check.py`): the
  cleaner only strips noise inside `<script>`/`<style>` tags and HTML
  comments. A page that renders a constantly-changing value directly into
  visible text (e.g. a live view counter) will still register as
  "changed" on every crawl — this is an acceptable, honest trade-off for
  a simple regex-based cleaner rather than a full HTML parser.

## Phase 3 — Scheduler + Multi-URL (✅ Done)

**What was built:**
- `crawler/scheduler.py`:
  - `load_urls(urls_file)` — reads `urls.json` fresh on every call (not
    cached at startup), so URLs added to the file while the scheduler is
    already running get picked up on the very next cycle without a
    restart. This matters because in Phase 5 the `/track` API endpoint
    will add URLs to this same file at runtime.
  - `run_crawl_cycle(urls_file)` — one full pass over every tracked URL:
    calls `crawl.capture_and_save()` per URL, logs a one-line result for
    each (fidelity status + saved/unchanged/failed), and returns a
    summary dict (`{total, saved, unchanged, failed, errors}`). A single
    URL raising an unexpected exception is caught and logged — it does
    NOT crash the cycle or the rest of the run.
  - `main()` — wraps `run_crawl_cycle` in an APScheduler `BlockingScheduler`
    with an `IntervalTrigger`, so it re-runs automatically forever on a
    fixed interval. Runs one cycle immediately on startup (so you're not
    staring at a blank terminal for a full interval before anything
    happens), then continues on the schedule.
  - `--once` flag: run a single cycle and exit immediately, without
    starting the recurring scheduler at all — this is what you want for
    quick manual testing or for a future "run once" API/cron trigger.

**How to run it manually:**
```bash
cd crawler
source venv/bin/activate
python scheduler.py --once                        # one cycle, then exit
python scheduler.py --interval-seconds 60          # recurring, every 60s (good for demos)
python scheduler.py                                 # recurring, default 300s (5 min) interval
```

**Verified during testing:**
- `--once` run twice in a row on the same URLs: first run saved all 3 as
  new, second run correctly reported `unchanged -> skipped saving` for
  every URL (same behavior as Phase 2, now via the scheduler's own
  logging path).
- Mixed a deliberately broken URL (nonexistent domain) into the list and
  confirmed the cycle logged it as `FAILED` and kept going — the other,
  valid URLs in the same cycle still captured successfully. The crawler
  never crashed.
- Ran the actual recurring scheduler (not `--once`) for multiple full
  intervals and confirmed via timestamped logs that it really does fire
  automatically on schedule (not just once): observed 5 separate cycles
  ~10 seconds apart with no manual intervention, and confirmed the WARC
  directory only gained new files on cycles where content had genuinely
  changed (Hacker News's front page rotating) vs. staying flat on cycles
  where nothing changed (`example.com`, `quotes.toscrape.com`).
- Verified dynamic URL list updates: started the scheduler with 3 URLs,
  appended a 4th URL to `urls.json` on disk while it was already running,
  and confirmed the very next cycle's log line said "4 tracked URL(s)"
  and actually crawled the new URL — no restart needed.
- **Found and fixed a real bug** during testing: the scheduler was
  configured with `add_job(..., next_run_time=None)`, intending "let the
  trigger decide the first run time". In APScheduler, `next_run_time=None`
  actually means "this job has no next run time" (i.e. permanently
  paused) — so the recurring job silently never fired after the initial
  manual kickoff. Fixed by omitting `next_run_time` entirely, which lets
  `add_job()` correctly compute the first fire time from the trigger.
  Caught this by watching a live run for a full interval-and-a-half and
  noticing no second cycle ever started.
- **Found and fixed a packaging issue** while stress-testing the Phase 1/2
  environment notes: the originally-pinned Playwright 1.47.0 only installs
  cleanly on Python 3.13 via a manual `pip install greenlet==<newer> &&
  pip install playwright --no-deps` workaround, because pip's dependency
  resolver enforces Playwright's exact-pinned `greenlet`/`pyee` versions
  and refuses to substitute newer ones on its own. That's not something a
  teammate running plain `pip install -r requirements.txt` would know to
  do. Fixed by bumping to Playwright `1.50.0`, the first release that
  relaxed those pins to flexible ranges — confirmed with a from-scratch
  venv (`rm -rf venv && python -m venv venv && pip install -r
  requirements.txt`, no manual steps) that it now installs cleanly in one
  command, then re-ran the full Phase 1/2/3 test suite against the rebuilt
  environment to confirm no behavior changed (identical content hashes,
  identical capture/skip decisions, scheduler still fires on interval).

## Phase 4 — pywb Replay Setup (✅ Done)

**What was built:**
- **pywb**, installed in its own Python **3.11** virtual environment
  (`pywb-data/venv/`) — a separate environment from the crawler/API's
  Python 3.13, because pywb's `gevent` dependency has no working build on
  3.13 (see "Environment notes" below for the full story).
- A pywb collection, `webapp-collection`, created via `wb-manager init`
  under `pywb-data/collections/webapp-collection/` (`archive/` for WARC
  files, `indexes/` for the `.cdxj` CDX index pywb uses to answer "what's
  the closest capture of this URL to this timestamp").
- `pywb-data/add_to_collection.sh` — a small bridge script. The crawler
  (Python 3.13) can't `import` pywb's own code (Python 3.11), so instead
  `crawl.py` shells out to this script as a subprocess; the script
  activates pywb's venv and runs `wb-manager add webapp-collection
  <warc-file>`, which copies the WARC into `archive/` and updates the CDX
  index — no restart of the running replay server is needed.
- `crawl.py`'s `register_with_pywb(warc_path)` — called automatically
  right after every new WARC is written, so a capture becomes replayable
  immediately. Best-effort: a pywb hiccup is logged to stderr but never
  undoes the fact that the WARC is already safely on disk.

**How to run it manually:**
```bash
cd pywb-data
source venv/bin/activate
wayback --port 8080 --bind 0.0.0.0     # start the replay server
# in another terminal, after a crawl has produced a WARC:
./add_to_collection.sh ../crawler/warc/example.com__20240115T120530000Z.warc.gz
```

**pywb's replay URL scheme** (used throughout the frontend, see
`frontend/src/api.js`'s `buildReplayUrl()`):
```
http://localhost:8080/webapp-collection/<14-digit-timestamp>/<original-url>       # framed replay (banner + link rewriting)
http://localhost:8080/webapp-collection/<14-digit-timestamp>id_/<original-url>    # raw replay (exact bytes, no rewriting)
```

**Verified during testing:**
- Ran a full replay cycle: captured a page, registered its WARC with
  pywb, then fetched the `id_` (raw) replay URL and confirmed it returned
  the *exact* originally-captured HTML byte-for-byte.
- Confirmed the framed (non-`id_`) replay URL returns pywb's link-rewritten
  page correctly wrapped in its Memento banner/frame.
- Confirmed CDX indexing works across multiple captures of the same URL
  (multiple lines in `index.cdxj`, one per snapshot) and that `wb-manager
  add` re-indexes without needing to restart the `wayback` server.

**Environment note:** this sandbox only has Python 3.13 available via
apt, but pywb pins `gevent==21.12.0`, whose C extension code targets
CPython internals (e.g. `PyThreadState.cframe`) that no longer exist in
3.13 — it cannot be built on 3.13 by any means (not even pre-installing a
compatible `greenlet` first). Since no 3.10/3.11 was available via apt
either, Python 3.11.10 was **compiled from source** (`./configure && make
-j2 && make install` to `/usr/local/python3.11`) specifically to build
pywb's venv. This is a one-time environment-setup step; a teammate on a
normal machine with `python3.11` already installed can skip straight to
`python3.11 -m venv venv && pip install pywb`.

## Phase 5 — Postgres Metadata DB + FastAPI (✅ Done)

**What was built:**
- `docs/schema.sql` — the `snapshots` table: `id, url, captured_at,
  content_hash, warc_path, http_status, capture_status` (+ an index on
  `(url, captured_at)` for fast per-URL lookups). Rows are only inserted
  for snapshots that were actually *saved* (i.e. content changed) — not
  every crawl attempt — matching the crawler's own save-skip logic.
- **Local Postgres** as a drop-in stand-in for Supabase: Supabase's core
  product IS hosted Postgres, handing you a normal connection string — so
  `api/db.py` just uses `psycopg2` against whatever `DATABASE_URL` is
  configured. Pointing it at a real Supabase project later is purely an
  env var change, zero code changes.
- `api/db.py` — `get_connection()` (context-managed, commit-or-rollback,
  fresh connection per call) plus `insert_snapshot()`,
  `get_snapshots_for_url()`, `get_closest_snapshot()` (nearest-date match
  via `ORDER BY ABS(EXTRACT(EPOCH FROM (captured_at - target)))`), and
  `get_all_tracked_urls_with_snapshots()`.
- `api/models.py` — Pydantic schemas for every endpoint's request/response
  shape (also what powers FastAPI's auto-generated `/docs` page).
- `api/main.py` — the FastAPI app, CORS enabled for all origins (safe
  here: no auth, no secrets, local dev tool), with the four spec
  endpoints:
  - `GET /snapshots?url=...` — every saved snapshot for a URL, oldest
    first (empty list, not a 404, if none exist yet).
  - `GET /snapshot/closest?url=...&date=...` — nearest snapshot to a
    date, either direction; 404 if the URL has no snapshots at all.
  - `GET /tracked-urls` / `POST /track` — read/write `crawler/urls.json`
    **directly**, completely independent of the DB. A URL is "tracked"
    the moment it's in that file, even before its first successful
    capture produces a DB row — and the crawler can keep working
    standalone with zero DB configured.
- `crawl.py`'s `record_snapshot_in_db()` — called right after every new
  WARC is saved (same best-effort pattern as pywb registration): inserts
  one row via `api/db.py`, logging (not raising) on any failure.

**How to run it manually:**
```bash
cd api
source venv/bin/activate
cp .env.example .env               # then fill in DATABASE_URL
uvicorn main:app --host 0.0.0.0 --port 8000
```

**Verified during testing:**
- Applied `schema.sql` to a local Postgres install and confirmed all four
  `db.py` functions (`insert_snapshot`, `get_snapshots_for_url`,
  `get_closest_snapshot`, `get_all_tracked_urls_with_snapshots`) work
  correctly against it via a direct Python test script.
- Started `uvicorn` and hit all four endpoints with `curl`:
  `GET /tracked-urls` returned the real `urls.json` contents;
  `POST /track` added a new URL idempotently (verified calling it twice
  with the same URL returned `already_tracked: true` the second time,
  without duplicating the entry); `GET /snapshots` / `GET
  /snapshot/closest` returned correct data once real snapshot rows
  existed (see Phase 7 below for the full pipeline test).
- Ran a real `python crawl.py <url>` capture against local Postgres and
  confirmed a new `snapshots` row was inserted automatically (`DB row id:
  1` printed in the capture summary), alongside the existing WARC file
  and pywb registration — proving Phases 2, 4, and 5 all fire together
  correctly from one crawl call.

## Phase 6 — React + Vite + Tailwind Frontend (✅ Done)

**What was built:**
- Scaffolded with `npm create vite@latest frontend -- --template react`,
  then added Tailwind CSS v3 (`darkMode: 'class'`, so our own toggle
  button controls the theme instead of only following the OS setting),
  `react-router-dom`, and `axios`.
- `src/api.js` — the **only** file that talks to the FastAPI backend.
  Wraps all four endpoints (`getSnapshots`, `getClosestSnapshot`,
  `getTrackedUrls`, `trackUrl`) plus `buildReplayUrl(url, capturedAt)`,
  which converts a stored ISO timestamp into pywb's required 14-digit
  `YYYYMMDDHHMMSS` replay-URL format.
- `src/hooks/useDarkMode.js` — persists the light/dark choice to
  `localStorage`, defaulting to the OS's `prefers-color-scheme` on first
  visit.
- `src/components/Layout.jsx` — shared header/nav/dark-mode-toggle shell
  wrapping every page via React Router's `<Outlet />`.
- Shared state-display components used by every page:
  `LoadingSpinner.jsx`, `ErrorMessage.jsx` (with a retry button),
  `EmptyState.jsx` (for "nothing here yet, and that's normal" — distinct
  from a real error), and `StatusBadge.jsx` (colored success/partial/
  failed pill, matching `capture_status`).
- **Home** (`pages/Home.jsx`): `SearchBar.jsx` (track a new URL) +
  `TrackedUrlsList.jsx` (every tracked URL, each linking straight into
  its own Timeline view).
- **Timeline** (`pages/Timeline.jsx`): the URL under view lives in the
  `?url=` query param (via `useSearchParams`, so the page is
  bookmarkable/shareable). `TimelineSlider.jsx`'s slider value is the
  *index* into the snapshots array, not a raw date — every tick always
  lands exactly on a real snapshot. `SnapshotViewer.jsx` renders the
  metadata block, the warning text + `StatusBadge` for any non-"success"
  capture, and the pywb replay iframe itself.
- **Compare** (`pages/Compare.jsx`): two independent
  `CompareView.jsx` instances side by side, each with its own date
  picker calling `GET /snapshot/closest` and showing "closest capture is
  N days away" when the match isn't exact.
- `vite.config.js`: `server.allowedHosts: true` — needed because Vite 5+
  blocks requests whose `Host` header doesn't match a known dev host (a
  DNS-rebinding protection), and this sandbox's preview URL uses a
  dynamic proxy domain. Fine for local/sandbox dev; would be reconsidered
  for anything internet-facing.

**How to run it manually:**
```bash
cd frontend
cp .env.example .env    # adjust VITE_API_BASE / VITE_PYWB_BASE if needed
npm run dev              # or: npm run build && npm run preview
```

**Verified during testing:**
- `npm run build` completes cleanly with no errors.
- Loaded Home, Timeline (with a real tracked URL + real snapshots), and
  Compare in an actual browser (via Playwright) and confirmed **zero
  console errors** on all three pages when the frontend's `VITE_API_BASE`
  / `VITE_PYWB_BASE` point at matching-protocol (`http://localhost`)
  URLs — see the Phase 7 note below on the one sandbox-only artifact this
  surfaced.
- Confirmed dark mode toggling actually flips the theme (not just
  following the OS setting) and survives a page reload.

## Phase 7 — Full Integration Test (✅ Done)

Ran every layer together via PM2 (`ecosystem.config.cjs`: `api` on 8000,
`pywb` on 8080, `frontend` on 5173) and exercised the complete path:

1. **Tracked a URL from the UI** — used Home's `SearchBar` to `POST
   /track` a URL, confirmed it appeared in `TrackedUrlsList` immediately
   and was appended to `crawler/urls.json` on disk.
2. **Ran a real multi-URL crawl** — `python scheduler.py --once` against
   all 3 tracked URLs. All three: fidelity-classified `SUCCESS`, hashed
   as new content, WARC-saved, registered with pywb, **and** inserted
   into Postgres — confirmed via the scheduler's own log line
   (`3 saved, 0 unchanged, 0 failed, 0 errored`) and by querying the
   `snapshots` table directly afterward.
3. **Timeline page against real data** — loaded `/timeline?url=...` for
   one of the crawled URLs in an actual browser; the slider, metadata
   block, `success` status badge, and the pywb replay iframe all rendered
   correctly with zero JS console errors.
4. **Compare page against real data** — picked a date on the Compare
   page and confirmed `GET /snapshot/closest` returned the right snapshot
   with a correct `days_off` value, and its replay iframe rendered.
5. **pywb replay verified directly** — fetched both the raw (`id_`) and
   framed replay URLs for a real crawled JS-heavy site
   (`news.ycombinator.com`) via `curl`, confirming HTTP 200 and non-empty
   HTML in both cases.

**Found and fixed one issue while testing (sandbox-specific, not a code
bug):** when the frontend's `VITE_PYWB_BASE` was pointed at this sandbox's
public **HTTPS** preview URL for pywb, the browser blocked pywb's own
banner-frame assets (`wb_frame.js`, `bootstrap.min.css`, etc.) as **mixed
content**, because those assets are hardcoded by pywb as absolute
`http://` URLs. This is purely an artifact of the sandbox's HTTPS-proxied
preview domain — on a real laptop, both the frontend dev server and pywb
run on plain `http://localhost`, so there's no protocol mismatch and this
does not occur. Confirmed by testing both services over matching
`http://localhost` URLs: zero console errors, replay iframe renders
correctly. **Also found and fixed a real (non-sandbox-specific) bug**:
`api/db.py`'s `load_dotenv()` call had no explicit path, so when
`crawl.py` (in `crawler/`) imported it, dotenv searched upward from the
*crawler's* working directory and never found `api/.env` at all. Fixed by
passing an explicit path built from `db.py`'s own file location.

## Roadmap

- [x] **Phase 1** — Crawler: Playwright capture + fidelity checks
- [x] **Phase 2** — Change detection (SHA-256) + WARC storage
- [x] **Phase 3** — Scheduler + multi-URL recurring crawls
- [x] **Phase 4** — pywb setup for WARC replay
- [x] **Phase 5** — Supabase Postgres + FastAPI endpoints
- [x] **Phase 6** — React + Vite + Tailwind frontend (Home / Timeline / Compare)
- [x] **Phase 7** — Full integration test (crawler → API → pywb → frontend)

## Running Everything Together

```bash
# One-time setup (see each phase's section above for details):
#   - crawler/venv, api/venv (Python 3.13), pywb-data/venv (Python 3.11)
#   - local Postgres (or Supabase) with docs/schema.sql applied
#   - api/.env and frontend/.env configured from their .env.example files
#   - frontend/node_modules installed (npm install)

pm2 start ecosystem.config.cjs      # starts api (8000), pywb (8080), frontend (5173)
pm2 logs --nostream                  # check all three services' logs

# Trigger a crawl manually (not managed by PM2 — run on demand or via cron):
cd crawler && source venv/bin/activate && python scheduler.py --once

# Or run it continuously in the background instead of the manual step above:
python scheduler.py --interval-seconds 300
```

Then open `http://localhost:5173` in a browser.

## Constraints

- 100% free/open-source stack, no paid APIs.
- Designed to run locally on a normal laptop.
- Tracked URL list intentionally kept small — this is a scoped demo, not a
  general-purpose web archive.
