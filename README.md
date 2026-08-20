# BoligScanner

An autonomous real-estate investment screener for the Norwegian housing market (FINN.no). It watches for new listings, pulls in the underlying disclosure documents, scores each property on rental yield/cash flow, and pushes qualifying deals to Slack — with no manual step required once it's running.

Built to solve a real problem: manually opening every new FINN.no listing, digging up the disclosure PDF, and running the rental-yield math by hand doesn't scale when dozens of listings appear per day. This project automates that entire pipeline end to end, running unattended in the background.

## Highlights

- **Fully autonomous pipeline** — from "new email in inbox" to "Slack alert with the numbers," no human in the loop.
- **Resilient document discovery** — a multi-tier fallback (FINN's own link → broker site search → HTML parsing) so a missing link on one source doesn't stop the pipeline.
- **Deterministic PDF parsing, no LLM** — building-condition (TG) grades are extracted with rule-based pattern matching: fast, free, and auditable, since the goal is a specific, verifiable field rather than a generic summary.
- **OAuth2 lifecycle management** — a daily health check verifies the Gmail token independently of the scan, so auth failures surface before they silently break ingestion.
- **UI/worker separation** — all business logic lives in a Streamlit-free core module, so the same functions back both the interactive dashboard and a headless background worker.
- **Multi-service containerized deployment** — UI and worker run as separate services from the same image via `docker-compose`, each with its own health check.

## What it does

1. **Listens for new listings** — polls Gmail for FINN.no saved-search alert emails and extracts the listing URLs.
2. **Scrapes listing data** from FINN.no (price, size, rooms, fees, location, etc.).
3. **Geocodes the address** and calculates distance to the nearest schools.
4. **Fetches the disclosure documents** ("salgsoppgave" / "tilstandsrapport") — first via FINN's direct link, falling back to searching the broker's own website when FINN doesn't expose one.
5. **Parses the PDF deterministically** to pull out building-condition ratings (TG grades) per component (roof, bathroom, kitchen, windows, etc.) — plain keyword/pattern matching, no AI/LLM involved.
6. **Runs the numbers**: rental yield, monthly cash flow, and capital required, against user-defined thresholds and market rent assumptions.
7. **Alerts on Slack** when a listing clears the bar, with the key numbers and a link to the documents.
8. **Logs everything** (scan runs, Gmail health, download attempts) to a local SQLite database, and can optionally sync processed results to a separate read-only dashboard.

It runs completely unattended: a background worker process performs the daily scan and a Gmail health check on a schedule, independent of whether anyone ever opens the web UI.

## Tech stack

| Purpose | Technology |
|---|---|
| UI | [Streamlit](https://streamlit.io/) |
| Language | Python 3.11 |
| Storage | SQLite |
| Listing ingestion | Gmail API (OAuth2, `google-api-python-client`) |
| Web scraping | `requests` + `BeautifulSoup` |
| Geocoding | [OpenRouteService](https://openrouteservice.org/) Geocoding API |
| PDF parsing | `pypdf` |
| Scheduling | `APScheduler` |
| Notifications | Slack Incoming Webhooks |
| Deployment | Docker / docker-compose |

## Architecture

The app is deliberately split so the "always-on" logic doesn't depend on a browser session ever connecting to Streamlit:

- **`boligscan_core.py`** — all core logic (scraping, Gmail, geocoding, Slack, financial calculations, DB schema). Contains no Streamlit UI code, so it can be imported and run from anywhere.
- **`app.py`** — the interactive Streamlit UI: sliders, buttons, tables, manual controls.
- **`pages/`** — additional Streamlit pages: a system dashboard (Gmail health, scan history), a Slack test-message sender, and an editor for the market-rent assumptions used in the yield calculation.
- **`worker.py`** — a standalone background process (started as its own service in `docker-compose.yml`) that runs the daily scan and Gmail health check on a schedule, using the same core functions as the UI.
- **`salgsoppgave_downloader.py` / `broker_site_fallback.py` / `broker_document_parser.py`** — the document-discovery pipeline: try FINN's direct link first, then fall back to finding and parsing the broker's own listing page.
- **`document_parser.py`** — rule-based extraction of building-condition data from the downloaded PDF.
- **`dashboard_sync.py`** — one-way sync of processed listings into a separate, presentation-only dashboard database.
- **`gmail_healthcheck.py`** — a lightweight daily check that the Gmail OAuth token is still valid, without touching any email content.

## Configuration

No credentials are stored in the repository. The app reads all secrets from **Streamlit's `secrets.toml`** (`.streamlit/secrets.toml`, gitignored) and local credential files, both of which live only on the host machine / deployment volume:

| Secret | Used for |
|---|---|
| `credentials.json` / `token.json` | Gmail API OAuth2 (read-only access to listing alert emails) |
| `ORS_API_KEY` | OpenRouteService geocoding |
| `SLACK_WEBHOOK_URL` / `SLACK_WEBHOOK_URL_AGDER` | Slack alert delivery |

Example `.streamlit/secrets.toml`:

```toml
ORS_API_KEY = "your-openrouteservice-key"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."
SLACK_WEBHOOK_URL_AGDER = "https://hooks.slack.com/services/..."
```

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Running with Docker

```bash
docker compose up -d
```

This starts two services from the same image: the interactive Streamlit UI (`boligscanner`) and the always-on background worker (`boligscanner-worker`) that performs the daily scan and health check.

## Author

Built by Olav Leek as a personal project to automate apartment search.
