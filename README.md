# Arabic by M.S — Stremio Subtitle Addon

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
│   ├── routes_subtitles.py   # cache-first /subtitles/{type}/{id}.json
│   ├── routes_download.py    # /download/{id}.srt (cached arabic or sample)
│   └── routes_companion.py   # /companion (HTML) + upload + list + translate
├── services/
│   ├── __init__.py
│   ├── cache_db.py            # SQLite metadata
│   ├── gemini_service.py      # Gemini REST client (env-driven)
│   ├── subdl_service.py       # SubDL search/import provider
│   ├── subsource_service.py   # SubSource search/import provider
│   └── translation_service.py # Orchestrates translate pipeline
├── utils/
│   ├── __init__.py
│   ├── srt_validator.py       # Filename + content validation
│   ├── hash_utils.py          # SHA-256 helpers
│   ├── srt_chunker.py         # Parse / render / chunk SRT
│   ├── srt_cleaner.py         # Parse Gemini's numbered replies
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
│   └── test_subsource.py      # Phase 6 mocked SubSource search/import
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
| `GET /subtitles/{type}/{id}.json`             | Cache-first Arabic lookup, sample fallback    |
| `GET /download/{id}.srt`                      | Serves cached Arabic or the bundled sample    |
| `GET /companion`                              | HTML page: upload + list + translate          |
| `POST /companion/upload-srt`                  | Upload an English `.srt`                      |
| `GET /companion/list`                         | JSON list of every uploaded record            |
| `GET /companion/search-subdl`                 | Search SubDL for English subtitles            |
| `POST /companion/import-subdl`                | Import a SubDL subtitle into the local cache  |
| `GET /companion/subsource-status`             | SubSource configuration status                |
| `GET /companion/search-subsource`             | Search SubSource for English subtitles        |
| `POST /companion/import-subsource`            | Import a SubSource subtitle into local cache  |
| `POST /companion/translate/{record_id}`       | Translate that record's English SRT to Arabic |

## Installing the addon in Stremio

1. Start the server (see above).
2. Open Stremio (web or desktop).
3. Go to **Add-ons → Community add-ons → Install via URL**.
4. Paste `http://127.0.0.1:8787/manifest.json` and click **Install**.
5. Play any movie or episode and open the subtitle picker.

> If Stremio runs on another device, replace `127.0.0.1` with your LAN IP
> (or a tunnel URL) and set `PUBLIC_BASE_URL` in `.env` accordingly.

## Configuring Gemini (Phase 6)

1. Copy `.env.example` to `.env`.
2. Set `GEMINI_API_KEY=...` to a key from <https://aistudio.google.com/app/apikey>.
3. Optionally set `GEMINI_MODEL=gemini-2.5-flash` (default), `gemini-2.5-pro`, etc.
4. Restart the server. Open `/companion`, click **Translate** on an
   uploaded row. The Arabic SRT is written to `cache/arabic/` and the
   row's status updates to *translated*.

If `GEMINI_API_KEY` is missing the translate endpoint returns **400** with
a clear `"GEMINI_API_KEY is not set"` message; malformed Gemini output
returns **502**. Translation failures are stored as `status="failed"` with
an `error_message`, and the companion page exposes a retry button.

## Configuring SubDL and SubSource

1. Copy `.env.example` to `.env`.
2. Set `SUBDL_API_KEY=...` to use `GET /companion/search-subdl` and
   `POST /companion/import-subdl`.
3. Set `SUBSOURCE_API_KEY=...` to use `GET /companion/search-subsource` and
   `POST /companion/import-subsource`.
4. Leave `SUBDL_BASE_URL` and `SUBSOURCE_BASE_URL` at their defaults unless
   you need to point at a different API environment.
5. Restart the server and open `/companion`.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

All Gemini, SubDL, and SubSource calls in tests are mocked — no live API
traffic is made.
