"""Local companion: HTML upload page + JSON upload/list/translate endpoints.

Phase 2 added upload + list. Phase 3 adds POST /companion/translate/{record_id}
which feeds the cached English SRT to Gemini and writes an Arabic SRT to
cache/arabic/.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from . import config
from services.cache_db import insert_subtitle, list_subtitles
from services.gemini_service import GeminiError, GeminiNotConfiguredError
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


# ---------------------------------------------------------------------------
# HTML companion page
# ---------------------------------------------------------------------------

_COMPANION_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Arabic by M.S — Companion</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif;
           max-width: 820px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }
    h1 { margin-bottom: 0.25rem; }
    .sub { color: #6b7280; margin-top: 0; }
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
    code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; }
    .empty { color: #6b7280; font-style: italic; }
    .row-msg { font-size: 0.8rem; margin-top: 0.25rem; }
  </style>
</head>
<body>
  <h1>Arabic by M.S — Companion</h1>
  <p class="sub">Upload an English <code>.srt</code>, then translate it to Arabic via Gemini.</p>

  <form id="form" enctype="multipart/form-data">
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

  <div id="result"></div>

  <h2>Uploaded subtitles</h2>
  <div id="list" class="empty">Loading…</div>

  <script>
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"
      })[c]);
    }

    async function translateRecord(recordId, btn) {
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = 'Translating…';
      const msg = document.getElementById('row-msg-' + recordId);
      if (msg) msg.innerHTML = '';
      try {
        const res = await fetch('/companion/translate/' + recordId, { method: 'POST' });
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
          const action = r.arabic_srt_path
            ? '<span class="badge done">Arabic available</span>'
            : '<button onclick="_translateRecord(' + r.id + ', this)">Translate</button>';
          return `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.video_id)}</td>
              <td>${escapeHtml(r.video_type)}</td>
              <td>${escapeHtml(r.release_name || '')}</td>
              <td><span class="badge ${r.status === 'translated' ? 'done' : 'pending'}">${escapeHtml(r.status)}</span></td>
              <td>${action}<div class="row-msg" id="row-msg-${r.id}"></div></td>
              <td>${escapeHtml(r.created_at)}</td>
            </tr>`;
        }).join('');
        list.className = '';
        list.innerHTML = `<table>
          <thead><tr>
            <th>#</th><th>Video ID</th><th>Type</th><th>Release</th>
            <th>Status</th><th>Action</th><th>Created (UTC)</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
      } catch (err) {
        document.getElementById('list').textContent = 'Failed to load list: ' + err.message;
      }
    }

    document.getElementById('form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const result = document.getElementById('result');
      result.innerHTML = '';
      try {
        const res = await fetch('/companion/upload-srt', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = `<div class="msg err">${escapeHtml(data.detail || 'Upload failed')}</div>`;
        } else {
          result.innerHTML = `<div class="msg ok">Uploaded as record #${data.id} (status: ${data.status}).</div>`;
          e.target.reset();
          await refreshList();
        }
      } catch (err) {
        result.innerHTML = `<div class="msg err">${escapeHtml(err.message)}</div>`;
      }
    });

    refreshList();
  </script>
</body>
</html>
"""


@router.get("/companion", response_class=HTMLResponse)
def companion_page() -> HTMLResponse:
    """Serve the simple HTML upload page."""
    return HTMLResponse(_COMPANION_HTML)


# ---------------------------------------------------------------------------
# Upload + list endpoints
# ---------------------------------------------------------------------------

_SAFE_VIDEO_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_video_id(video_id: str) -> str:
    """Sanitize a video_id so it's safe to use in a filename."""
    return _SAFE_VIDEO_ID.sub("_", video_id.strip()) or "unknown"


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

    english_hash = sha256_text(text)
    english_dir = config.ENGLISH_CACHE_DIR
    english_dir.mkdir(parents=True, exist_ok=True)
    target = english_dir / f"{_slug_video_id(video_id)}_{english_hash[:12]}.srt"
    target.write_text(text, encoding="utf-8")

    record_id = insert_subtitle(
        config.DB_PATH,
        video_id=video_id.strip(),
        video_type=(video_type or "movie").strip() or "movie",
        release_name=release_name.strip() if release_name else None,
        english_srt_path=str(target),
        english_srt_hash=english_hash,
        arabic_srt_path=None,
        status="uploaded",
    )

    return JSONResponse(
        {
            "id": record_id,
            "video_id": video_id.strip(),
            "video_type": (video_type or "movie").strip() or "movie",
            "release_name": release_name.strip() if release_name else None,
            "english_srt_path": str(target),
            "english_srt_hash": english_hash,
            "arabic_srt_path": None,
            "status": "uploaded",
        }
    )


@router.get("/companion/list")
def companion_list() -> Dict[str, Any]:
    """Return all uploaded subtitle records as JSON."""
    return {"items": list_subtitles(config.DB_PATH)}


# ---------------------------------------------------------------------------
# Translate endpoint (Phase 3)
# ---------------------------------------------------------------------------


@router.post("/companion/translate/{record_id}")
def translate_endpoint(record_id: int) -> JSONResponse:
    """Translate the English SRT for `record_id` and persist the Arabic file."""
    try:
        updated = translate_record(
            config.DB_PATH,
            record_id,
            arabic_cache_dir=config.ARABIC_CACHE_DIR,
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
            detail=f"Translator returned unusable output: {exc}",
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
        }
    )
