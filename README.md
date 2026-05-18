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
* **Phase 13** — subtitle preview, timing offsets, and preferred record
  management. You can now preview cached English/Arabic cues, shift subtitle
  timing without losing the previous file, save short notes such as
  release-group hints, and mark the preferred translated record for the exact
  movie or episode.
* **Phase 14** — safe one-click prepare workflow. The companion can now
  search all configured providers, import the best English SRT, and start a
  background Gemini translation in one action. Optional auto-prepare can also
  trigger this workflow from `/subtitles` without blocking Stremio.
* **Phase 15** — usage guardrails, quota safety, and duplicate-cost
  prevention. The addon now tracks local provider/Gemini usage in SQLite,
  enforces safe daily limits, reuses already-running expensive jobs, and
  shows usage counters and events in Companion.
* **Phase 16** — safe batch episode prepare queue. The companion can now
  queue a bounded season/episode range, skip episodes that already have
  real Arabic, reuse active prepare/translation work, process items one by
  one in the background, cancel safely, and inspect batch status without
  bypassing Phase 15 guardrails.
* **Phase 17** — OpenSubtitles provider integration. The companion and
  provider router now support OpenSubtitles as an optional third external
  English subtitle provider, while preserving the existing SubDL, SubSource,
  Gemini, one-click prepare, usage guard, and batch prepare workflows.
* **Phase 18** — provider reliability hardening and transparent diagnostics.
  Shared retry handling now protects provider search/download calls from
  transient HTTP failures, `search-all` returns structured safe provider
  error details, batch prepare surfaces provider-safe item errors through
  the router, and the Companion UI exposes a minimal Provider Diagnostics
  panel without changing the existing Phase 16/17 workflows.
* **Phase 19** — subtitle match intelligence and transparent best-choice
  explanations. Ranked provider results now expose score breakdowns,
  confidence, and safe warnings, `search-all` surfaces the extra match
  context, and `import-best` explains why the winning subtitle was chosen
  without changing provider APIs or the Phase 18 retry/diagnostics flow.
* **Phase 20** — subtitle quality inspection and safe auto-rejection hints.
  Imported provider subtitles are now checked for malformed timing,
  overlaps, repeated text, suspicious size, and likely wrong language after
  download and before save/translate. Companion import responses and batch
  prepare item status expose non-blocking quality metadata and warnings.
Nvidia is **not** wired up yet.
SubDL, SubSource, and OpenSubtitles are the currently supported external
search/import providers.

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
│   ├── batch_prepare_service.py # Phase 16 batch episode prepare queue
│   ├── prepare_service.py     # One-click prepare workflow + dedupe
│   ├── provider_router.py     # Unified subtitle provider router
│   ├── opensubtitles_service.py # OpenSubtitles search/import provider
│   ├── subdl_service.py       # SubDL search/import provider
│   ├── subsource_service.py   # SubSource search/import provider
│   ├── usage_guard.py         # Daily limits + usage event tracking
│   └── translation_service.py # Orchestrates translate pipeline
├── utils/
│   ├── __init__.py
│   ├── srt_validator.py       # Filename + content validation
│   ├── hash_utils.py          # SHA-256 helpers
│   ├── srt_chunker.py         # Parse / render / chunk SRT
│   ├── srt_cleaner.py         # Parse Gemini's numbered replies
│   ├── srt_quality.py         # Translation cleanup + subtitle quality analysis
│   ├── srt_timing.py          # Safe SRT timestamp shifting helpers
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
│   ├── test_phase12_episode_identity.py # Phase 12 episode-aware matching
│   ├── test_phase13_preview_timing_preferred.py # Phase 13 preview/timing/preferred
│   ├── test_phase14_prepare_workflow.py # Phase 14 prepare workflow
│   ├── test_phase15_usage_guard.py # Phase 15 quota safety + usage tracking
│   ├── test_phase16_batch_prepare.py # Phase 16 batch prepare queue
│   ├── test_phase17_opensubtitles_provider.py # Phase 17 OpenSubtitles provider
│   ├── test_phase18_provider_reliability.py # Phase 18 provider reliability
│   ├── test_phase19_match_intelligence.py # Phase 19 match intelligence
│   └── test_phase20_srt_quality_gate.py   # Phase 20 subtitle quality gate
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
| `GET /companion/provider-diagnostics`         | Safe provider status, retry settings, and usage counters |
| `GET /companion/diagnostics`                  | Local diagnostics for common setup issues     |
| `GET /companion/subdl-status`                 | SubDL configuration status                    |
| `GET /companion/search-subdl`                 | Search SubDL for English subtitles            |
| `POST /companion/import-subdl`                | Import a SubDL subtitle into the local cache  |
| `GET /companion/subsource-status`             | SubSource configuration status                |
| `GET /companion/search-subsource`             | Search SubSource for English subtitles        |
| `POST /companion/import-subsource`            | Import a SubSource subtitle into local cache  |
| `GET /companion/opensubtitles-status`         | OpenSubtitles configuration status            |
| `GET /companion/search-opensubtitles`         | Search OpenSubtitles for English subtitles    |
| `POST /companion/import-opensubtitles`        | Import an OpenSubtitles subtitle into local cache |
| `GET /companion/search-all`                   | Search all configured providers together with confidence and warnings |
| `POST /companion/import-best`                 | Import the highest-ranked English subtitle, explain why it won, expose quality warnings, and optionally auto-translate it |
| `POST /companion/prepare`                     | Search, import, and background-translate in one action |
| `GET /companion/prepare-status/{canonical_video_key}` | Show exact-title prepare readiness, active job, and latest error |
| `POST /companion/batch-prepare`               | Queue a safe sequential batch prepare job for series episodes |
| `GET /companion/batch-status/{batch_id}`      | Show batch status, item states, and usage warning |
| `POST /companion/cancel-batch/{batch_id}`     | Cancel queued batch items safely |
| `GET /companion/batch-list`                   | Show latest batch prepare jobs |
| `GET /companion/usage-status`                | Show today's local quota counters, limits, and remaining counts |
| `GET /companion/usage-events`                | Show recent local usage events |
| `POST /companion/clear-usage-events`         | Clear usage-event history only |
| `POST /companion/translate/{record_id}`       | Translate that record's English SRT to Arabic |
| `POST /companion/translate-background/{record_id}` | Queue a background translation job        |
| `GET /companion/translation-status/{record_id}` | Translation progress / error state for one record |
| `GET /companion/job-status/{job_id}`          | Background translation job status + progress |
| `GET /companion/preview/{record_id}`          | Preview cached English or Arabic subtitle cues |
| `POST /companion/adjust-timing/{record_id}`   | Shift subtitle timing for English, Arabic, or both |
| `POST /companion/set-preferred/{record_id}`   | Prefer one translated Arabic record for that exact canonical video |
| `POST /companion/update-note/{record_id}`     | Save a short user note for one record |
| `POST /companion/test-gemini`                 | Optional tiny Gemini smoke test               |
| `POST /companion/self-test`                   | Safe local DB/cache/translation-status self-test |

## Supported Stremio IDs

The addon now understands these Stremio video-id shapes:

* `tt1234567` for movies
* `tt1234567:1:5` for series episodes

Canonical cache keys are stored as:

* movie: `tt1234567`
* episode: `tt1234567:s01e05`

Phase 13 builds on that exact canonical identity. Preferred-record selection
and subtitle matching are scoped to the exact movie or episode key, so one
episode can never take another episode's translated Arabic subtitle.
Phase 14 uses that same canonical key for one-click prepare and for
duplicate-prevention when auto-prepare is enabled.
Phase 15 keeps that exact matching and adds local usage guardrails so
duplicate prepare/translation attempts do not burn quota twice.
Phase 16 keeps the same canonical matching for season/episode batch runs,
so each queued item still maps to one exact episode key such as
`tt1234567:s01e05`.

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

## Configuring SubDL, SubSource, and OpenSubtitles

1. Copy `.env.example` to `.env`.
2. Set `SUBDL_API_KEY=...` to use `GET /companion/search-subdl` and
   `POST /companion/import-subdl`.
3. Set `SUBSOURCE_API_KEY=...` to use `GET /companion/search-subsource` and
   `POST /companion/import-subsource`.
4. Set `OPENSUBTITLES_API_KEY=...` and `OPENSUBTITLES_USER_AGENT=...` to use
   `GET /companion/search-opensubtitles` and
   `POST /companion/import-opensubtitles`.
5. Leave `SUBDL_BASE_URL`, `SUBSOURCE_BASE_URL`, and
   `OPENSUBTITLES_BASE_URL` at their defaults unless you need a different
   API environment.
6. Restart the server and open `/companion`.
7. Use **Search All Providers** to hit all configured providers together, or click
   **Import Best** to store the highest-ranked English SRT immediately. If
   Gemini is configured, the same form can auto-translate it after import.
8. For series, you can pass either `video_id=tt1234567:1:5` or
   `video_id=tt1234567` together with `season=1` and `episode=5`.
9. Open **Preview English** or **Preview Arabic** from the companion list to
   inspect cues before choosing a preferred record or applying a timing offset.
10. Use **Adjust Timing** or the quick `+500ms` / `-500ms` Arabic actions to
   shift subtitle timing while preserving the previous file.
11. Use **Set Preferred** to pin the translated Arabic record Stremio should
    serve first for that exact canonical movie or episode.
12. Use **Prepare Arabic Subtitle** to search providers, import the best
     English subtitle, and queue a background Arabic translation in one action.
13. Use **Batch Prepare Episodes** to queue a small episode range for the
    same series/season while reusing active work and skipping already-ready
    Arabic episodes.
14. Use **Usage Guard** to inspect daily Gemini/provider usage, recent usage
    events, and clear usage-event history without touching subtitle records.

## Auto-Prepare

Set `AUTO_PREPARE_ON_SUBTITLES_REQUEST=true` only if you want `/subtitles`
requests to trigger background prepare automatically when no exact Arabic
subtitle exists yet. This never blocks the Stremio response, but it can
consume provider and Gemini quota, so the default is `false`.

Phase 15 adds local daily guardrails, and Phase 16 adds a safe batch-size cap:

* `MAX_DAILY_GEMINI_TRANSLATIONS=20`
* `MAX_DAILY_PROVIDER_SEARCHES=100`
* `MAX_DAILY_PREPARE_REQUESTS=50`
* `MAX_BATCH_PREPARE_ITEMS=10`
* `ALLOW_AUTO_PREPARE_WHEN_LIMITED=false`

Leave `ALLOW_AUTO_PREPARE_WHEN_LIMITED=false` unless you explicitly want
auto-prepare to keep consuming provider/Gemini quota after the local daily
limits are reached.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

All Gemini, SubDL, SubSource, and OpenSubtitles calls in tests are mocked — no live API
traffic is made.
