"""Local companion: manual upload, provider import, and Gemini translation."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from . import config
from services import subdl_service, subsource_service
from services.cache_db import insert_subtitle, list_subtitles
from services.gemini_service import (
    GeminiError,
    GeminiNotConfiguredError,
    get_status as get_gemini_status,
)
from services.subdl_service import SubDLError, SubDLNotConfiguredError
from services.subsource_service import SubSourceError, SubSourceNotConfiguredError
from services.translation_service import (
    EnglishFileMissingError,
    RecordNotFoundError,
    TranslationError,
    translate_record,
)
from utils.hash_utils import sha256_text
from utils.srt_cleaner import TranslationFormatError
from utils.srt_validator import (
    SRTValidationError,
    validate_srt_content,
    validate_srt_filename,
)

router = APIRouter()


_COMPANION_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Arabic by M.S — Companion</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif;
           max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }
    h1 { margin-bottom: 0.25rem; }
    h2 { margin-top: 0; }
    .sub { color: #6b7280; margin-top: 0; }
    .section { margin-top: 2rem; }
    .provider-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1rem; }
    form { background: #f9fafb; padding: 1rem 1.25rem; border-radius: 8px;
           border: 1px solid #e5e7eb; }
    label { display: block; margin: 0.6rem 0 0.25rem; font-weight: 600; font-size: 0.9rem; }
    input, select { width: 100%; padding: 0.5rem; box-sizing: border-box;
                    border: 1px solid #d1d5db; border-radius: 4px; font-size: 0.95rem; }
    button { padding: 0.45rem 0.9rem; background: #2563eb; color: white;
             border: 0; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
    button.primary { padding: 0.6rem 1.2rem; margin-top: 1rem; font-size: 0.95rem; }
    button:disabled { background: #9ca3af; cursor: wait; }
    button:hover:not(:disabled) { background: #1d4ed8; }
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }
    th, td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #e5e7eb;
             text-align: left; vertical-align: top; }
    th { background: #f3f4f6; }
    .msg { padding: 0.6rem 0.8rem; border-radius: 4px; margin: 1rem 0; font-size: 0.9rem; }
    .ok  { background: #d1fae5; color: #065f46; }
    .err { background: #fee2e2; color: #991b1b; }
    .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px;
             font-size: 0.75rem; font-weight: 600; }
    .badge.done { background: #d1fae5; color: #065f46; }
    .badge.pending { background: #fef3c7; color: #92400e; }
    .badge.failed { background: #fee2e2; color: #991b1b; }
    code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; }
    .empty { color: #6b7280; font-style: italic; }
    .row-msg { font-size: 0.8rem; margin-top: 0.25rem; }
    .status-panel { margin: 1rem 0; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
  </style>
</head>
<body>
  <h1>Arabic by M.S — Companion</h1>
  <p class="sub">Upload an English <code>.srt</code> or import one from SubDL or SubSource, then translate it to Arabic via Gemini.</p>

  <div id="gemini-status" class="status-panel msg">Checking Gemini configuration…</div>
  <div id="subdl-status" class="status-panel msg">Checking SubDL configuration…</div>
  <div id="subsource-status" class="status-panel msg">Checking SubSource configuration…</div>

  <div class="section">
    <h2>Provider search</h2>
    <div class="provider-grid">
      <div>
        <form id="subdl-form">
          <h2>Search SubDL</h2>
          <label for="subdl_video_id">Video ID <span style="color:#dc2626">*</span> (IMDb preferred)</label>
          <input id="subdl_video_id" name="video_id" required placeholder="tt1234567" />

          <label for="subdl_video_type">Video type</label>
          <select id="subdl_video_type" name="video_type">
            <option value="movie" selected>movie</option>
            <option value="series">series</option>
          </select>

          <label for="subdl_season">Season (optional)</label>
          <input id="subdl_season" name="season" inputmode="numeric" placeholder="1" />

          <label for="subdl_episode">Episode (optional)</label>
          <input id="subdl_episode" name="episode" inputmode="numeric" placeholder="1" />

          <label for="subdl_query">Query fallback (optional)</label>
          <input id="subdl_query" name="query" placeholder="Series name or release string" />

          <label for="subdl_language">Language</label>
          <input id="subdl_language" name="language" value="EN" />

          <label for="subdl_release_name">Preferred release name (optional)</label>
          <input id="subdl_release_name" name="release_name" placeholder="Some.Show.S01E01.1080p.WEB-DL" />

          <button type="submit" class="primary">Search SubDL</button>
        </form>
        <div id="subdl-result"></div>
        <div id="subdl-results" class="empty">No SubDL search run yet.</div>
      </div>

      <div>
        <form id="subsource-form">
          <h2>Search SubSource</h2>
          <label for="subsource_video_id">Video ID <span style="color:#dc2626">*</span> (IMDb preferred)</label>
          <input id="subsource_video_id" name="video_id" required placeholder="tt1234567" />

          <label for="subsource_video_type">Video type</label>
          <select id="subsource_video_type" name="video_type">
            <option value="movie" selected>movie</option>
            <option value="series">series</option>
          </select>

          <label for="subsource_season">Season (optional)</label>
          <input id="subsource_season" name="season" inputmode="numeric" placeholder="1" />

          <label for="subsource_episode">Episode (optional)</label>
          <input id="subsource_episode" name="episode" inputmode="numeric" placeholder="1" />

          <label for="subsource_query">Query fallback (optional)</label>
          <input id="subsource_query" name="query" placeholder="Series name or release string" />

          <label for="subsource_language">Language</label>
          <input id="subsource_language" name="language" value="en" />

          <label for="subsource_release_name">Preferred release name (optional)</label>
          <input id="subsource_release_name" name="release_name" placeholder="Some.Show.S01E01.1080p.WEB-DL" />

          <button type="submit" class="primary">Search SubSource</button>
        </form>
        <div id="subsource-result"></div>
        <div id="subsource-results" class="empty">No SubSource search run yet.</div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Manual upload</h2>
    <form id="upload-form" enctype="multipart/form-data">
      <label for="video_id">Video ID <span style="color:#dc2626">*</span> (e.g. <code>tt1234567</code>)</label>
      <input id="video_id" name="video_id" required placeholder="tt1234567" />

      <label for="video_type">Video type</label>
      <select id="video_type" name="video_type">
        <option value="movie" selected>movie</option>
        <option value="series">series</option>
      </select>

      <label for="release_name">Release name (optional)</label>
      <input id="release_name" name="release_name" placeholder="Some.Movie.2024.1080p.WEB-DL" />

      <label for="srt_file">English .srt file <span style="color:#dc2626">*</span></label>
      <input id="srt_file" name="srt_file" type="file" accept=".srt" required />

      <button type="submit" class="primary">Upload</button>
    </form>
  </div>

  <div id="result"></div>

  <div class="section">
    <h2>Uploaded subtitles</h2>
    <div id="list" class="empty">Loading…</div>
  </div>

  <script>
    let geminiStatus = { configured: false, model: '', message: 'Checking Gemini configuration…' };
    let subdlStatus = __SUBDL_STATUS_JSON__;
    let subsourceStatus = __SUBSOURCE_STATUS_JSON__;
    window._subdlItems = [];
    window._subsourceItems = [];

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"
      })[c]);
    }

    function renderGeminiStatus(status) {
      geminiStatus = status;
      const el = document.getElementById('gemini-status');
      const klass = status.configured ? 'msg ok' : 'msg err';
      el.className = 'status-panel ' + klass;
      el.innerHTML =
        '<strong>Gemini status:</strong> ' +
        escapeHtml(status.configured ? 'Configured' : 'Not configured') +
        ' (<code>' + escapeHtml(status.model) + '</code>)<br />' +
        escapeHtml(status.message);
    }

    function renderProviderStatus(elementId, label, status, keyName) {
      const el = document.getElementById(elementId);
      const klass = status.configured ? 'msg ok' : 'msg err';
      el.className = 'status-panel ' + klass;
      el.innerHTML =
        '<strong>' + escapeHtml(label) + ' status:</strong> ' +
        escapeHtml(status.configured ? 'Configured' : 'Not configured') +
        ' (<code>' + escapeHtml(status.base_url) + '</code>)<br />' +
        escapeHtml(status.message || (keyName + ' is missing.'));
    }

    function renderSubdlStatus(status) {
      subdlStatus = status;
      renderProviderStatus('subdl-status', 'SubDL', status, 'SUBDL_API_KEY');
    }

    function renderSubsourceStatus(status) {
      subsourceStatus = status;
      renderProviderStatus('subsource-status', 'SubSource', status, 'SUBSOURCE_API_KEY');
    }

    async function refreshGeminiStatus() {
      try {
        const res = await fetch('/companion/gemini-status');
        const data = await res.json();
        renderGeminiStatus(data);
      } catch (err) {
        renderGeminiStatus({
          configured: false,
          model: 'unknown',
          message: 'Failed to load Gemini status: ' + err.message
        });
      }
    }

    async function refreshSubsourceStatus() {
      try {
        const res = await fetch('/companion/subsource-status');
        const data = await res.json();
        renderSubsourceStatus(data);
      } catch (err) {
        renderSubsourceStatus({
          configured: false,
          base_url: 'unknown',
          message: 'Failed to load SubSource status: ' + err.message
        });
      }
    }

    async function translateRecord(recordId, btn, force) {
      const msg = document.getElementById('row-msg-' + recordId);
      if (!geminiStatus.configured) {
        if (msg) msg.innerHTML = '<span class="msg err">' + escapeHtml(geminiStatus.message) + '</span>';
        return;
      }

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = 'Translating…';
      if (msg) msg.innerHTML = '';
      try {
        const suffix = force ? '?force=true' : '';
        const res = await fetch('/companion/translate/' + recordId + suffix, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
          if (msg) msg.innerHTML = '<span class="msg err">' + escapeHtml(data.detail || 'Translate failed') + '</span>';
        }
        await refreshList();
      } catch (err) {
        if (msg) msg.innerHTML = '<span class="msg err">' + escapeHtml(err.message) + '</span>';
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    window._translateRecord = translateRecord;

    async function importProviderResult(itemsName, index, status, resultElementId, endpoint, videoIdInputId, videoTypeInputId, releaseNameInputId, btn) {
      const box = document.getElementById(resultElementId);
      if (!status.configured) {
        box.innerHTML = '<div class="msg err">' + escapeHtml(status.message) + '</div>';
        return;
      }

      const item = window[itemsName][index];
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = 'Importing…';
      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            video_id: document.getElementById(videoIdInputId).value,
            video_type: document.getElementById(videoTypeInputId).value,
            release_name: item.release_name || document.getElementById(releaseNameInputId).value || null,
            download_url: item.download_url
          })
        });
        const data = await res.json();
        if (!res.ok) {
          box.innerHTML = '<div class="msg err">' + escapeHtml(data.detail || 'Import failed') + '</div>';
        } else {
          box.innerHTML = '<div class="msg ok">Imported as record #' + escapeHtml(data.id) + '.</div>';
          await refreshList();
        }
      } catch (err) {
        box.innerHTML = '<div class="msg err">' + escapeHtml(err.message) + '</div>';
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }

    async function importSubdlResult(index, btn) {
      return importProviderResult(
        '_subdlItems',
        index,
        subdlStatus,
        'subdl-result',
        '/companion/import-subdl',
        'subdl_video_id',
        'subdl_video_type',
        'subdl_release_name',
        btn
      );
    }
    window._importSubdlResult = importSubdlResult;

    async function importSubsourceResult(index, btn) {
      return importProviderResult(
        '_subsourceItems',
        index,
        subsourceStatus,
        'subsource-result',
        '/companion/import-subsource',
        'subsource_video_id',
        'subsource_video_type',
        'subsource_release_name',
        btn
      );
    }
    window._importSubsourceResult = importSubsourceResult;

    function renderProviderResults(nodeId, items, importFnName, emptyText) {
      const node = document.getElementById(nodeId);
      if (!items || items.length === 0) {
        node.className = 'empty';
        node.textContent = emptyText;
        return;
      }
      node.className = '';
      node.innerHTML = `<table>
        <thead><tr>
          <th>Provider</th><th>Subtitle ID</th><th>Language</th><th>Release</th>
          <th>Score</th><th>Action</th>
        </tr></thead>
        <tbody>${items.map((item, index) => `
          <tr>
            <td>${escapeHtml(item.provider)}</td>
            <td>${escapeHtml(item.subtitle_id || '')}</td>
            <td>${escapeHtml(item.language || '')}</td>
            <td>${escapeHtml(item.release_name || '')}</td>
            <td>${escapeHtml(String(item.score || 0))}</td>
            <td><button onclick="${importFnName}(${index}, this)">Import</button></td>
          </tr>`).join('')}
        </tbody>
      </table>`;
    }

    async function refreshList() {
      try {
        const res = await fetch('/companion/list');
        const data = await res.json();
        const list = document.getElementById('list');
        if (!data.items || data.items.length === 0) {
          list.className = 'empty';
          list.textContent = 'No subtitles uploaded yet.';
          return;
        }
        const rows = data.items.map(r => {
          const badgeClass = r.status === 'translated'
            ? 'done'
            : (r.status === 'failed' ? 'failed' : 'pending');
          let action = '<button onclick="_translateRecord(' + r.id + ', this, false)">Translate</button>';
          if (r.status === 'failed') {
            action = '<button onclick="_translateRecord(' + r.id + ', this, false)">Retry Translate</button>';
          } else if (r.arabic_srt_path) {
            action = '<span class="badge done">Arabic available</span>';
          }
          return `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.video_id)}</td>
              <td>${escapeHtml(r.video_type)}</td>
              <td>${escapeHtml(r.release_name || '')}</td>
              <td><span class="badge ${badgeClass}">${escapeHtml(r.status)}</span></td>
              <td>${escapeHtml(r.error_message || '')}</td>
              <td><div class="actions">${action}</div><div class="row-msg" id="row-msg-${r.id}"></div></td>
              <td>${escapeHtml(r.created_at)}</td>
            </tr>`;
        }).join('');
        list.className = '';
        list.innerHTML = `<table>
          <thead><tr>
            <th>#</th><th>Video ID</th><th>Type</th><th>Release</th>
            <th>Status</th><th>Error</th><th>Action</th><th>Created (UTC)</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
      } catch (err) {
        document.getElementById('list').textContent = 'Failed to load list: ' + err.message;
      }
    }

    async function searchProvider(formId, status, resultId, resultsId, itemsName, endpoint, emptyText) {
      const result = document.getElementById(resultId);
      result.innerHTML = '';
      if (!status.configured) {
        result.innerHTML = '<div class="msg err">' + escapeHtml(status.message) + '</div>';
        return;
      }

      const params = new URLSearchParams();
      for (const [key, value] of new FormData(document.getElementById(formId)).entries()) {
        if (String(value).trim()) {
          params.set(key, String(value).trim());
        }
      }

      try {
        const res = await fetch(endpoint + '?' + params.toString());
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = '<div class="msg err">' + escapeHtml(data.detail || 'Provider search failed') + '</div>';
          renderProviderResults(resultsId, [], '_noop', emptyText);
          return;
        }
        window[itemsName] = data.items || [];
        renderProviderResults(resultsId, window[itemsName], itemsName === '_subdlItems' ? '_importSubdlResult' : '_importSubsourceResult', emptyText);
        result.innerHTML = '<div class="msg ok">Found ' + escapeHtml(String(window[itemsName].length)) + ' result(s).</div>';
      } catch (err) {
        result.innerHTML = '<div class="msg err">' + escapeHtml(err.message) + '</div>';
      }
    }

    document.getElementById('subdl-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      await searchProvider(
        'subdl-form',
        subdlStatus,
        'subdl-result',
        'subdl-results',
        '_subdlItems',
        '/companion/search-subdl',
        'No SubDL results found.'
      );
    });

    document.getElementById('subsource-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      await searchProvider(
        'subsource-form',
        subsourceStatus,
        'subsource-result',
        'subsource-results',
        '_subsourceItems',
        '/companion/search-subsource',
        'No SubSource results found.'
      );
    });

    document.getElementById('upload-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const result = document.getElementById('result');
      result.innerHTML = '';
      try {
        const res = await fetch('/companion/upload-srt', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = '<div class="msg err">' + escapeHtml(data.detail || 'Upload failed') + '</div>';
        } else {
          result.innerHTML = '<div class="msg ok">Uploaded as record #' + escapeHtml(data.id) + ' (status: ' + escapeHtml(data.status) + ').</div>';
          e.target.reset();
          await refreshList();
        }
      } catch (err) {
        result.innerHTML = '<div class="msg err">' + escapeHtml(err.message) + '</div>';
      }
    });

    refreshGeminiStatus();
    renderSubdlStatus(subdlStatus);
    renderSubsourceStatus(subsourceStatus);
    refreshSubsourceStatus();
    refreshList();
  </script>
</body>
</html>
"""


@router.get("/companion", response_class=HTMLResponse)
def companion_page() -> HTMLResponse:
    """Serve the companion HTML page."""
    html = _COMPANION_HTML.replace(
        "__SUBDL_STATUS_JSON__",
        json.dumps(subdl_service.get_status()),
    ).replace(
        "__SUBSOURCE_STATUS_JSON__",
        json.dumps(subsource_service.get_status()),
    )
    return HTMLResponse(html)


_SAFE_VIDEO_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_video_id(video_id: str) -> str:
    """Sanitize a video_id so it's safe to use in a filename."""
    return _SAFE_VIDEO_ID.sub("_", video_id.strip()) or "unknown"


def _store_english_srt_record(
    *,
    video_id: str,
    video_type: str,
    release_name: Optional[str],
    text: str,
) -> Dict[str, Any]:
    """Persist a validated English SRT and return the record payload."""
    english_hash = sha256_text(text)
    english_dir = config.ENGLISH_CACHE_DIR
    english_dir.mkdir(parents=True, exist_ok=True)
    target = english_dir / "{0}_{1}.srt".format(
        _slug_video_id(video_id),
        english_hash[:12],
    )
    target.write_text(text, encoding="utf-8")

    normalized_video_id = video_id.strip()
    normalized_video_type = (video_type or "movie").strip() or "movie"
    normalized_release_name = release_name.strip() if release_name else None
    record_id = insert_subtitle(
        config.DB_PATH,
        video_id=normalized_video_id,
        video_type=normalized_video_type,
        release_name=normalized_release_name,
        english_srt_path=str(target),
        english_srt_hash=english_hash,
        arabic_srt_path=None,
        status="uploaded",
    )

    return {
        "id": record_id,
        "video_id": normalized_video_id,
        "video_type": normalized_video_type,
        "release_name": normalized_release_name,
        "english_srt_path": str(target),
        "english_srt_hash": english_hash,
        "arabic_srt_path": None,
        "status": "uploaded",
        "error_message": None,
    }


def _parse_import_payload(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalize provider import payload fields."""
    return {
        "video_id": str(payload.get("video_id") or "").strip(),
        "video_type": str(payload.get("video_type") or "movie").strip() or "movie",
        "release_name": (
            str(payload.get("release_name")).strip()
            if payload.get("release_name") not in (None, "")
            else None
        ),
        "download_url": str(payload.get("download_url") or "").strip(),
    }


async def _read_request_payload(request: Request) -> Dict[str, Any]:
    """Accept either JSON or form-encoded provider import bodies."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form)


@router.post("/companion/upload-srt")
async def upload_srt(
    video_id: str = Form(..., description="e.g. tt1234567"),
    video_type: str = Form("movie"),
    release_name: Optional[str] = Form(None),
    srt_file: UploadFile = File(...),
) -> JSONResponse:
    """Validate and store an uploaded English SRT file."""
    if not video_id or not video_id.strip():
        raise HTTPException(status_code=400, detail="video_id is required")

    filename = srt_file.filename or ""
    try:
        validate_srt_filename(filename)
    except SRTValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw = await srt_file.read()
    try:
        text = validate_srt_content(raw)
    except SRTValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(
        _store_english_srt_record(
            video_id=video_id,
            video_type=video_type,
            release_name=release_name,
            text=text,
        )
    )


@router.get("/companion/list")
def companion_list() -> Dict[str, Any]:
    """Return all uploaded subtitle records as JSON."""
    return {"items": list_subtitles(config.DB_PATH)}


@router.get("/companion/gemini-status")
def gemini_status() -> Dict[str, Any]:
    """Return whether Gemini translation is currently configured."""
    return get_gemini_status()


@router.get("/companion/subsource-status")
def subsource_status() -> Dict[str, Any]:
    """Return whether SubSource search/import is currently configured."""
    return subsource_service.get_status()


@router.get("/companion/search-subdl")
def search_subdl(
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    query: Optional[str] = Query(None),
    language: str = Query("EN"),
    release_name: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Search SubDL and return normalized subtitle candidates."""
    try:
        items = subdl_service.search_subtitles(
            video_id=video_id,
            video_type=video_type,
            season=season,
            episode=episode,
            query=query,
            language=language,
            release_name=release_name,
        )
    except SubDLNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubDLError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": items}


@router.get("/companion/search-subsource")
def search_subsource(
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    query: Optional[str] = Query(None),
    language: str = Query("en"),
    release_name: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Search SubSource and return normalized subtitle candidates."""
    try:
        items = subsource_service.search_subtitles(
            video_id=video_id,
            video_type=video_type,
            season=season,
            episode=episode,
            query=query,
            language=language,
            release_name=release_name,
        )
    except SubSourceNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": items}


@router.post("/companion/import-subdl")
async def import_subdl(request: Request) -> JSONResponse:
    """Download an English SRT from SubDL, validate it, and add a DB record."""
    payload = _parse_import_payload(await _read_request_payload(request))
    if not payload["video_id"]:
        raise HTTPException(status_code=400, detail="video_id is required")
    if not payload["download_url"]:
        raise HTTPException(status_code=400, detail="download_url is required")

    try:
        raw = subdl_service.download_subtitle_data(str(payload["download_url"]))
        text = validate_srt_content(raw)
    except SubDLNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SRTValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubDLError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(
        _store_english_srt_record(
            video_id=str(payload["video_id"]),
            video_type=str(payload["video_type"]),
            release_name=payload["release_name"],
            text=text,
        )
    )


@router.post("/companion/import-subsource")
async def import_subsource(request: Request) -> JSONResponse:
    """Download an English SRT from SubSource, validate it, and add a DB record."""
    payload = _parse_import_payload(await _read_request_payload(request))
    if not payload["video_id"]:
        raise HTTPException(status_code=400, detail="video_id is required")
    if not payload["download_url"]:
        raise HTTPException(status_code=400, detail="download_url is required")

    try:
        raw = subsource_service.download_subtitle_data(str(payload["download_url"]))
        text = validate_srt_content(raw)
    except SubSourceNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SRTValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(
        _store_english_srt_record(
            video_id=str(payload["video_id"]),
            video_type=str(payload["video_type"]),
            release_name=payload["release_name"],
            text=text,
        )
    )


@router.post("/companion/translate/{record_id}")
def translate_endpoint(record_id: int, force: bool = Query(False)) -> JSONResponse:
    """Translate the English SRT for `record_id` and persist the Arabic file."""
    try:
        updated = translate_record(
            config.DB_PATH,
            record_id,
            arabic_cache_dir=config.ARABIC_CACHE_DIR,
            force=force,
        )
    except GeminiNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EnglishFileMissingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TranslationFormatError as exc:
        raise HTTPException(
            status_code=502,
            detail="Translator returned unusable output: {0}".format(exc),
        ) from exc
    except GeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TranslationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        {
            "id": updated.get("id"),
            "video_id": updated.get("video_id"),
            "video_type": updated.get("video_type"),
            "status": updated.get("status"),
            "arabic_srt_path": updated.get("arabic_srt_path"),
            "error_message": updated.get("error_message"),
        }
    )
