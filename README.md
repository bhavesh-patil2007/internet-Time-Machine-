# Internet Time Machine

A lightweight web archiving and time-travel playback system. Track a list of URLs, periodically capture snapshots (including JavaScript-rendered pages), store only meaningfully-changed versions, and browse any tracked site as it looked on a past date through a simple timeline UI.

Inspired by the Wayback Machine's Memento protocol, built as a scaled-down, open-source student project.

## Team

- **Bhavesh Patil** — Backend (crawler, change detection, storage, API)
- **[Teammate name]** — Frontend (React UI, timeline slider, comparison view)

## Tech Stack

| Layer | Technology |
|---|---|
| Crawler | Python + Playwright |
| Change detection | SHA-256 hashing |
| Archive format | WARC (via `warcio`) |
| Storage + playback | pywb |
| Metadata DB | PostgreSQL (Supabase) |
| API | FastAPI |
| Frontend | React |

## Project Structure
internet-time-machine/
├── crawler/ # Playwright-based crawler + change detection
├── webapp/
│ ├── api/ # FastAPI backend
│ └── frontend/ # React frontend
├── pywb-data/ # WARC storage + pywb collection
└── docs/ # Project synopsis, literature review, paper drafts

## How to Run Locally

### Crawler
cd crawler
pip install -r requirements.txt
playwright install
python crawl.py

### API
cd webapp/api
pip install -r requirements.txt
uvicorn main:app --reload

### Frontend
cd webapp/frontend
npm install
npm run dev

## Status

🚧 In active development

## License

MIT
