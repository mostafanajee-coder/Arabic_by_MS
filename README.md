# Arabic by M.S - Stremio Subtitle Addon

A FastAPI Stremio subtitle addon that serves Arabic subtitles.

* **Phase 1** — base addon skeleton: manifest, subtitles, download endpoints,
  bundled sample Arabic SRT.
* **Phase 2** — local companion page for uploading English SRT files, SQLite
  cache (`cache/subtitles.db`), cache-first lookup in the subtitles endpoint.
* **Phase 3** — Gemini-powered English→Arabic translation. A *Translate*
  button on the companion page calls `POST /companion/translate/{record_id}`,
  which feeds the cached English SRT to Gemini, saves the Arabic file under
  `cache/arabic/`, and updates the DB record so `/subtitles/...` starts
  serving the translated file.
* **Phase 5** — SubDL search/import foundation. The companion page can search
  SubDL, import an English subtitle result into the shared upload cache, and
  then send that imported record through the same Gemini translation flow.
* **Phase 6** — SubSource search/import. The companion page now supports both
  SubDL and SubSource as external English subtitle providers, while keeping
  manual upload, Gemini translation, and cache-first subtitle serving intact.
* **Phase 7** — unified provider router and one-click workflow. The companion
  page can search SubDL and SubSource together, rank and deduplicate combined
  results, import the best English SRT with one click, store provider metadata
  in SQLite, and optionally translate that imported subtitle immediately with
  Gemini.
* **Phase 8** — hardened Gemini translation for real subtitle files. Long SRTs
  are translated in validated chunks with per-record progress tracking, safer
  retry behavior, cleanup of stray Gemini formatting, and Arabic SRT quality
  checks before the translated file is saved.
* **Phase 9** — local Stremio integration hardening. The addon now exposes
  `/health`, install info, diagnostics, a safe self-test, an optional tiny
  Gemini smoke test, and request-aware subtitle URLs so local installs match
  the actual host and port Stremio is using.
* **Phase 10** — background translation jobs. Long Gemini translations can be
  queued to run in the background, polled live from the companion page, and
  safely deduplicated per record so local use remains practical.
* **Phase 11** — production-ready Stremio subtitle response behavior. The
  addon now returns a real cached Arabic subtitle only when one actually
  exists. Otherwise it returns an honest generated `Arabic by M.S - Status`
  subtitle that tells the user whether they need to upload, translate, retry,
  or wait for a running background job.
* **Phase 12** — episode-aware Stremio identity and canonical matching. The
  addon now parses both `tt1234567` and `tt1234567:1:5`, stores canonical
  video identity in SQLite, and only returns cached Arabic subtitles for the
  exact movie or episode that was translated/imported.

Nvidia and OpenSubtitles are **not** wired up yet.
SubDL and SubSource are the currently supported external search/import providers.

## Project layout

```
Arabic_by_MS/
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── manifest.py
│   ├── config.py
│   ├── routes_status.py      # /status-subtitle/{video_id}.srt
│   ├── routes_subtitles.py   # cache-first /subtitles/{type}/{id}.json
│   ├── routes_download.py    # /download/{id}.srt (cached arabic or sample/demo)
│   └── routes_companion.py   # /companion (HTML) + upload + list + translate
├── services/
│   ├── __init__.py
│   ├── cache_db.py            # SQLite metadata
│   ├── gemini_service.py      # Gemini REST client (env-driven)
│   ├── job_manager.py         # Local background translation jobs
│   ├── provider_router.py     # Unified SubDL + SubSource router
│   ├── subdl_service.py       # SubDL search/import provider
│   ├── subsource_service.py   # SubSource search/import provider
│   └── translation_service.py # Orchestrates translate pipeline
├── utils/
│   ├── __init__.py
│   ├── srt_validator.py       # Filename + content validation
│   ├── hash_utils.py          # SHA-256 helpers
│   ├── srt_chunker.py         # Parse / render / chunk SRT
│   ├── srt_cleaner.py         # Parse Gemini's numbered replies
│   ├── srt_quality.py         # Arabic translation cleanup + validation
│   ├── stremio_id.py          # Parse Stremio movie / episode ids safely
│   ├── status_srt.py          # Generated Stremio-facing status subtitles
│   └── subtitle_matcher.py    # Provider result scoring / ranking
├── cache/
│   ├── arabic/sample_arabic.srt
│   ├── english/               # User uploads land here
│   └── subtitles.db           # Created on first upload
├── tests/
│   ├── conftest.py
│   ├── test_api.py            # Phase 1 endpoint behavior
│   ├── test_srt_validator.py  # Phase 2 validator
│   ├── test_cache_db.py       # Phase 2 SQLite layer
│   ├── test_upload.py         # Phase 2 upload + cache fallback
│   ├── test_translation.py    # Phase 3 mocked Gemini translation
│   ├── test_subdl.py          # Phase 5 mocked SubDL search/import
│   ├── test_subsource.py      # Phase 6 mocked SubSource search/import
│   ├── test_phase7_provider_router.py  # Phase 7 unified router/import-best
│   ├── test_phase9_local_integration.py # Phase 9 local health/install/diagnostics
│   ├── test_phase10_background_jobs.py # Phase 10 background job polling
│   └── test_phase12_episode_identity.py # Phase 12 episode-aware matching
├── requirements.txt
├── run.bat
├── .env.example
└── README.md
```

## Running locally (Windows)

```bat
run.bat
```

The script creates `.venv`, installs `requirements.txt`, and starts:

```
uvicorn backend.main:app --host 0.0.0.0 --port 8787 --reload
```

## Running locally (manual)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8787
```

## Endpoints

| Endpoint                                      | Purpose                                       |
| --------------------------------------------- | --------------------------------------------- |
| `GET /`                                       | Friendly landing                              |
| `GET /manifest.json`                          | Stremio manifest                              |
| `GET /subtitles/{type}/{id}.json`             | Real Arabic when cached, otherwise a status subtitle |
| `GET /status-subtitle/{video_id}.srt`         | Generated Arabic status SRT for Stremio       |
| `GET /download/{id}.srt`                      | Serves cached Arabic or the bundled sample/demo file |
| `GET /health`                                 | App + local cache/DB readiness                |
| `GET /companion`                              | HTML page: upload + provider search + translate |
| `POST /companion/upload-srt`                  | Upload an English `.srt`                      |
| `GET /companion/list`                         | JSON list of every uploaded record            |
| `GET /companion/install-info`                 | Local manifest / companion URLs for Stremio install |
| `GET /companion/provider-status`              | Combined Gemini + provider configuration status |
| `GET /companion/diagnostics`                  | Local diagnostics for common setup issues     |
| `GET /companion/subdl-status`                 | SubDL configuration status                    |
| `GET /companion/search-subdl`                 | Search SubDL for English subtitles            |
| `POST /companion/import-subdl`                | Import a SubDL subtitle into the local cache  |
| `GET /companion/subsource-status`             | SubSource configuration status                |
| `GET /companion/search-subsource`             | Search SubSource for English subtitles        |
| `POST /companion/import-subsource`            | Import a SubSource subtitle into local cache  |
| `GET /companion/search-all`                   | Search SubDL + SubSource together             |
| `POST /companion/import-best`                 | Import the highest-ranked English subtitle and optionally auto-translate it |
| `POST /companion/translate/{record_id}`       | Translate that record's English SRT to Arabic |
| `POST /companion/translate-background/{record_id}` | Queue a background translation job        |
| `GET /companion/translation-status/{record_id}` | Translation progress / error state for one record |
| `GET /companion/job-status/{job_id}`          | Background translation job status + progress |
| `POST /companion/test-gemini`                 | Optional tiny Gemini smoke test               |
| `POST /companion/self-test`                   | Safe local DB/cache/translation-status self-test |

## Supported Stremio IDs

The addon now understands these Stremio video-id shapes:

* `tt1234567` for movies
* `tt1234567:1:5` for series episodes

Canonical cache keys are stored as:

* movie: `tt1234567`
* episode: `tt1234567:s01e05`

## Installing the addon in Stremio

1. Start the server (see above).
2. Open Stremio (web or desktop).
3. Go to **Add-ons → Community add-ons → Install via URL**.
4. Paste `http://127.0.0.1:8787/manifest.json` and click **Install**.
5. Play any movie or episode and open the subtitle picker.

For many local Windows setups, `http://localhost:8787/manifest.json` also
works. Phase 9 adds `GET /companion/install-info`, which shows the exact
manifest URL and companion URL for the host/port you are currently using.
Phase 11 also makes the Stremio-facing behavior honest: if no translated
Arabic file is cached yet, Stremio will show `Arabic by M.S - Status` until
the real Arabic subtitle is ready. Phase 12 adds exact movie/episode matching,
so `/subtitles/movie/tt1234567.json` and `/subtitles/series/tt1234567:1:5.json`
resolve independently.

> If Stremio runs on another device, replace `127.0.0.1` with your LAN IP
> (or a tunnel URL) and set `PUBLIC_BASE_URL` in `.env` accordingly.

## Configuring Gemini

1. Copy `.env.example` to `.env`.
2. Set `GEMINI_API_KEY=...` to a key from <https://aistudio.google.com/app/apikey>.
3. Optionally set `GEMINI_MODEL=gemini-2.5-flash` (default), `gemini-2.5-pro`, etc.
4. Restart the server. Open `/companion`, click **Translate** on an
   uploaded row. The Arabic SRT is translated chunk by chunk, validated,
   written to `cache/arabic/`, and the row's status updates to *translated*.

If `GEMINI_API_KEY` is missing the translate endpoint returns **400** with
a clear `"GEMINI_API_KEY is not set"` message; malformed Gemini output
returns **502**. Translation failures are stored as `status="failed"` with
an `error_message`. Phase 8 also persists `progress_total_chunks`,
`progress_done_chunks`, and `progress_message`, which are exposed through
`GET /companion/translation-status/{record_id}` and shown on the companion
page during translation. Existing translated files are reused unless
`force=true` is requested.

## Configuring SubDL and SubSource

1. Copy `.env.example` to `.env`.
2. Set `SUBDL_API_KEY=...` to use `GET /companion/search-subdl` and
   `POST /companion/import-subdl`.
3. Set `SUBSOURCE_API_KEY=...` to use `GET /companion/search-subsource` and
   `POST /companion/import-subsource`.
4. Leave `SUBDL_BASE_URL` and `SUBSOURCE_BASE_URL` at their defaults unless
   you need to point at a different API environment.
5. Restart the server and open `/companion`.
6. Use **Search All Providers** to hit both providers together, or click
   **Import Best** to store the highest-ranked English SRT immediately. If
   Gemini is configured, the same form can auto-translate it after import.
7. For series, you can pass either `video_id=tt1234567:1:5` or
   `video_id=tt1234567` together with `season=1` and `episode=5`.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

All Gemini, SubDL, and SubSource calls in tests are mocked — no live API
traffic is made.
