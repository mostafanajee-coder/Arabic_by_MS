"""Local companion: upload, provider import, unified search, and Gemini translation."""

from __future__ import annotations

import json
import re
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from . import config
from .manifest import build_manifest
from .routes_subtitles import router as subtitles_router
from services import (
    gemini_service,
    job_manager,
    prepare_service,
    provider_router,
    subdl_service,
    subsource_service,
    usage_guard,
)
from services.cache_db import (
    delete_record,
    get_record,
    get_table_columns,
    init_db,
    insert_subtitle,
    list_subtitles,
    set_preferred_record,
    set_user_note,
    update_record_media,
)
from services.gemini_service import GeminiError, GeminiNotConfiguredError, get_status as get_gemini_status
from services.provider_router import PROVIDER_SUBDL, PROVIDER_SUBSOURCE
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
from utils.srt_chunker import SRTParseError, parse_srt
from utils.srt_quality import SRTQualityError
from utils.status_srt import build_status_srt
from utils.stremio_id import parse_stremio_video_id
from utils.srt_timing import SRTTimingError, shift_srt_content
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
  <title>Arabic by M.S - Companion</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif;
           max-width: 1180px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }
    h1 { margin-bottom: 0.25rem; }
    h2 { margin: 0 0 0.75rem; }
    .sub { color: #6b7280; margin-top: 0; }
    .section { margin-top: 2rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1rem; }
    form { background: #f9fafb; padding: 1rem 1.25rem; border-radius: 8px;
           border: 1px solid #e5e7eb; }
    label { display: block; margin: 0.6rem 0 0.25rem; font-weight: 600; font-size: 0.9rem; }
    input, select { width: 100%; padding: 0.5rem; box-sizing: border-box;
                    border: 1px solid #d1d5db; border-radius: 4px; font-size: 0.95rem; }
    input[type="checkbox"] { width: auto; margin-right: 0.45rem; }
    button { padding: 0.45rem 0.9rem; background: #2563eb; color: white;
             border: 0; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
    button.primary { padding: 0.6rem 1.2rem; margin-top: 1rem; font-size: 0.95rem; }
    button.secondary { background: #0f766e; }
    button:disabled { background: #9ca3af; cursor: wait; }
    button:hover:not(:disabled) { background: #1d4ed8; }
    button.secondary:hover:not(:disabled) { background: #0b5f59; }
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }
    th, td { padding: 0.45rem 0.6rem; border-bottom: 1px solid #e5e7eb;
             text-align: left; vertical-align: top; }
    th { background: #f3f4f6; }
    .msg { padding: 0.6rem 0.8rem; border-radius: 4px; margin: 1rem 0; font-size: 0.9rem; }
    .ok { background: #d1fae5; color: #065f46; }
    .err { background: #fee2e2; color: #991b1b; }
    .note { background: #eef2ff; color: #3730a3; }
    .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px;
             font-size: 0.75rem; font-weight: 600; }
    .badge.done { background: #d1fae5; color: #065f46; }
    .badge.pending { background: #fef3c7; color: #92400e; }
    .badge.failed { background: #fee2e2; color: #991b1b; }
    code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; }
    .empty { color: #6b7280; font-style: italic; }
    .status-panel { margin: 1rem 0; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    .check-row { margin-top: 0.9rem; display: flex; align-items: center; }
    .summary { margin-top: 0.75rem; }
  </style>
</head>
<body>
  <h1>Arabic by M.S - Companion</h1>
  <p class="sub">Upload an English <code>.srt</code>, search providers, or import the best ranked English subtitle and optionally translate it to Arabic with Gemini. Until a real Arabic file is ready, Stremio will show <code>Arabic by M.S - Status</code> instead of pretending a translation already exists.</p>

  <div id="install-info" class="status-panel msg note">
    <strong>Install in Stremio:</strong><br />
    Manifest URL<br />
    <input id="manifest-url" value="http://localhost:8787/manifest.json" readonly />
    <div class="summary">Open Stremio and install via URL using the manifest above.</div>
  </div>
  <div class="status-panel msg note">
    <strong>Stremio subtitle types:</strong><br />
    <code>Arabic by M.S</code> = a real cached Arabic subtitle file is ready.<br />
    <code>Arabic by M.S - Status</code> = a generated guidance subtitle telling you to upload, translate, retry, or wait.
  </div>
  <div id="health-status" class="status-panel msg">Checking local health...</div>
  <div id="gemini-status" class="status-panel msg">Checking Gemini configuration...</div>
  <div id="subdl-status" class="status-panel msg">Checking SubDL configuration...</div>
  <div id="subsource-status" class="status-panel msg">Checking SubSource configuration...</div>

  <div class="section">
    <form id="prepare-form">
      <h2>Prepare Arabic Subtitle</h2>
      <div class="grid">
        <div>
          <label for="prepare_video_id">Video ID <span style="color:#dc2626">*</span></label>
          <input id="prepare_video_id" name="video_id" required placeholder="tt1234567 or tt1234567:1:5" />

          <label for="prepare_video_type">Video type</label>
          <select id="prepare_video_type" name="video_type">
            <option value="movie" selected>movie</option>
            <option value="series">series</option>
          </select>

          <label for="prepare_query">Query fallback (optional)</label>
          <input id="prepare_query" name="query" placeholder="Movie title or release string" />
        </div>
        <div>
          <label for="prepare_season">Season (optional)</label>
          <input id="prepare_season" name="season" inputmode="numeric" placeholder="1" />

          <label for="prepare_episode">Episode (optional)</label>
          <input id="prepare_episode" name="episode" inputmode="numeric" placeholder="5" />

          <label for="prepare_release_name">Preferred release name (optional)</label>
          <input id="prepare_release_name" name="release_name" placeholder="Some.Show.S01E05.1080p.WEB-DL" />

          <div class="check-row">
            <input id="prepare_force" name="force" type="checkbox" />
            <label for="prepare_force" style="margin:0;">Force prepare even if Arabic already exists</label>
          </div>
        </div>
      </div>
      <button type="submit" class="primary">Prepare Arabic Subtitle</button>
    </form>
    <div id="prepare-result"></div>
  </div>

  <div class="section">
    <form id="search-all-form">
      <h2>Search All Providers</h2>
      <div class="grid">
        <div>
          <label for="all_video_id">Video ID <span style="color:#dc2626">*</span> (IMDb preferred)</label>
          <input id="all_video_id" name="video_id" required placeholder="tt1234567" />

          <label for="all_video_type">Video type</label>
          <select id="all_video_type" name="video_type">
            <option value="movie" selected>movie</option>
            <option value="series">series</option>
          </select>

          <label for="all_query">Query fallback (optional)</label>
          <input id="all_query" name="query" placeholder="Movie title or release string" />

          <label for="all_language">Language</label>
          <input id="all_language" name="language" value="en" />
        </div>
        <div>
          <label for="all_season">Season (optional)</label>
          <input id="all_season" name="season" inputmode="numeric" placeholder="1" />

          <label for="all_episode">Episode (optional)</label>
          <input id="all_episode" name="episode" inputmode="numeric" placeholder="1" />

          <label for="all_release_name">Preferred release name (optional)</label>
          <input id="all_release_name" name="release_name" placeholder="Some.Show.S01E01.1080p.WEB-DL" />

          <div class="check-row">
            <input id="all_auto_translate" name="auto_translate" type="checkbox" />
            <label for="all_auto_translate" style="margin:0;">Auto translate after import</label>
          </div>
          <div class="check-row">
            <input id="all_background_translate" name="background_translate" type="checkbox" />
            <label for="all_background_translate" style="margin:0;">Run auto translate in background</label>
          </div>
        </div>
      </div>
      <div class="actions" style="margin-top: 1rem;">
        <button type="submit" class="primary">Search All Providers</button>
        <button type="button" id="import-best-btn" class="primary secondary">Import Best</button>
      </div>
    </form>
    <div id="search-all-result"></div>
    <div id="search-all-results" class="empty">No unified search run yet.</div>
  </div>

  <div class="section">
    <h2>Provider Search</h2>
    <div class="grid">
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
    <h2>Manual Upload</h2>
    <form id="upload-form" enctype="multipart/form-data">
      <label for="video_id">Video ID <span style="color:#dc2626">*</span> (e.g. <code>tt1234567</code> or <code>tt1234567:1:5</code>)</label>
      <input id="video_id" name="video_id" required placeholder="tt1234567 or tt1234567:1:5" />

      <label for="video_type">Video type</label>
      <select id="video_type" name="video_type">
        <option value="movie" selected>movie</option>
        <option value="series">series</option>
      </select>

      <label for="season">Season (optional)</label>
      <input id="season" name="season" inputmode="numeric" placeholder="1" />

      <label for="episode">Episode (optional)</label>
      <input id="episode" name="episode" inputmode="numeric" placeholder="5" />

      <label for="release_name">Release name (optional)</label>
      <input id="release_name" name="release_name" placeholder="Some.Movie.2024.1080p.WEB-DL" />

      <label for="srt_file">English .srt file <span style="color:#dc2626">*</span></label>
      <input id="srt_file" name="srt_file" type="file" accept=".srt" required />

      <button type="submit" class="primary">Upload</button>
    </form>
  </div>

  <div id="result"></div>

  <div class="section">
    <div class="grid">
      <div>
        <h2>Subtitle Preview</h2>
        <div id="preview-result" class="empty">No preview loaded yet.</div>
      </div>
      <div>
        <form id="timing-form">
          <h2>Adjust Timing</h2>
          <label for="timing_record_id">Record ID <span style="color:#dc2626">*</span></label>
          <input id="timing_record_id" name="record_id" required inputmode="numeric" placeholder="1" />

          <label for="timing_offset_ms">Offset ms <span style="color:#dc2626">*</span></label>
          <input id="timing_offset_ms" name="offset_ms" required inputmode="numeric" placeholder="500 or -500" />

          <label for="timing_target">Target</label>
          <select id="timing_target" name="target">
            <option value="arabic" selected>arabic</option>
            <option value="english">english</option>
            <option value="both">both</option>
          </select>

          <div class="check-row">
            <input id="timing_force" name="force" type="checkbox" />
            <label for="timing_force" style="margin:0;">Force overwrite current file paths</label>
          </div>

          <button type="submit" class="primary">Adjust Timing</button>
        </form>
        <div id="timing-result"></div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Usage Guard</h2>
    <div id="usage-status-panel" class="status-panel msg note">Loading usage counters...</div>
    <div id="usage-warning" class="empty">Auto-prepare is disabled.</div>
    <div class="actions" style="margin-top: 0.75rem;">
      <button type="button" id="clear-usage-events-btn" class="secondary">Clear Usage Events</button>
    </div>
    <div id="usage-clear-result"></div>
    <div id="usage-events" class="empty">Loading usage events...</div>
  </div>

  <div class="section">
    <h2>Uploaded Subtitles</h2>
    <div id="list" class="empty">Loading...</div>
  </div>

  <script>
    let geminiStatus = { configured: false, model: "", message: "Checking Gemini configuration..." };
    let subdlStatus = { configured: false, base_url: "", message: "Checking SubDL configuration..." };
    let subsourceStatus = { configured: false, base_url: "", message: "Checking SubSource configuration..." };
    window._subdlItems = [];
    window._subsourceItems = [];
    window._allItems = [];
    window._jobPollers = {};
    window._usageStatus = null;

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
      })[c]);
    }

    function readFormValues(formId) {
      const params = new URLSearchParams();
      for (const [key, value] of new FormData(document.getElementById(formId)).entries()) {
        const text = String(value).trim();
        if (text) {
          params.set(key, text);
        }
      }
      return params;
    }

    function readSearchAllPayload() {
      const params = readFormValues("search-all-form");
      return {
        video_id: params.get("video_id") || "",
        video_type: params.get("video_type") || "movie",
        season: params.get("season") || null,
        episode: params.get("episode") || null,
        query: params.get("query") || null,
        language: params.get("language") || "en",
        release_name: params.get("release_name") || null,
        auto_translate: document.getElementById("all_auto_translate").checked,
        background_translate: document.getElementById("all_background_translate").checked
      };
    }

    function renderInstallInfo(info) {
      const el = document.getElementById("install-info");
      const manifestInput = document.getElementById("manifest-url");
      const manifestUrl = info.manifest_url || "http://localhost:8787/manifest.json";
      manifestInput.value = manifestUrl;
      el.className = "status-panel msg note";
      el.innerHTML =
        "<strong>Install in Stremio:</strong><br />Manifest URL<br />" +
        "<input id='manifest-url' value='" + escapeHtml(manifestUrl) + "' readonly />" +
        "<div class='summary'>Base URL: <code>" + escapeHtml(info.base_url || "http://localhost:8787") + "</code><br />" +
        "Companion: <code>" + escapeHtml(info.companion_url || "") + "</code></div>";
    }

    function renderHealthStatus(health) {
      const el = document.getElementById("health-status");
      const ok = health.status === "ok";
      el.className = "status-panel " + (ok ? "msg ok" : "msg err");
      el.innerHTML =
        "<strong>Local health:</strong> " + escapeHtml(health.status || "unknown") +
        "<br />DB ready: " + escapeHtml(String(!!health.cache_db_ready)) +
        " | cache dirs ready: " + escapeHtml(String(!!health.cache_dirs_ready)) +
        " | status subtitle ready: " + escapeHtml(String(!!health.status_subtitle_ready));
    }

    function renderGeminiStatus(status) {
      geminiStatus = status;
      const el = document.getElementById("gemini-status");
      el.className = "status-panel " + (status.configured ? "msg ok" : "msg err");
      el.innerHTML =
        "<strong>Gemini status:</strong> " +
        escapeHtml(status.configured ? "Configured" : "Not configured") +
        " (<code>" + escapeHtml(status.model || "unknown") + "</code>)<br />" +
        escapeHtml(status.message || "");
    }

    function renderProviderStatus(elementId, label, status) {
      const el = document.getElementById(elementId);
      el.className = "status-panel " + (status.configured ? "msg ok" : "msg err");
      el.innerHTML =
        "<strong>" + escapeHtml(label) + " status:</strong> " +
        escapeHtml(status.configured ? "Configured" : "Not configured") +
        " (<code>" + escapeHtml(status.base_url || "unknown") + "</code>)<br />" +
        escapeHtml(status.message || "");
    }

    function renderSubdlStatus(status) {
      subdlStatus = status;
      renderProviderStatus("subdl-status", "SubDL", status);
    }

    function renderSubsourceStatus(status) {
      subsourceStatus = status;
      renderProviderStatus("subsource-status", "SubSource", status);
    }

    function renderUsageStatus(data) {
      window._usageStatus = data;
      const panel = document.getElementById("usage-status-panel");
      panel.className = "status-panel msg note";
      panel.innerHTML =
        "<strong>Today:</strong> " + escapeHtml(data.today || "") +
        "<br />Gemini translations: " + escapeHtml(String(data.gemini_translations_used || 0)) + "/" + escapeHtml(String(data.gemini_translations_limit || 0)) +
        " | Provider searches: " + escapeHtml(String(data.provider_searches_used || 0)) + "/" + escapeHtml(String(data.provider_searches_limit || 0)) +
        " | Prepare requests: " + escapeHtml(String(data.prepare_requests_used || 0)) + "/" + escapeHtml(String(data.prepare_requests_limit || 0)) +
        "<br />Remaining: Gemini " + escapeHtml(String(data.gemini_translations_remaining || 0)) +
        ", Provider " + escapeHtml(String(data.provider_searches_remaining || 0)) +
        ", Prepare " + escapeHtml(String(data.prepare_requests_remaining || 0));

      const warning = document.getElementById("usage-warning");
      if (data.auto_prepare_enabled) {
        warning.className = "msg note";
        warning.innerHTML =
          "<strong>Warning:</strong> auto-prepare on subtitle requests is enabled." +
          " Allow when limited: <code>" + escapeHtml(String(!!data.allow_auto_prepare_when_limited)) + "</code>.";
      } else {
        warning.className = "empty";
        warning.textContent = "Auto-prepare is disabled.";
      }
    }

    function renderUsageEvents(items) {
      const node = document.getElementById("usage-events");
      if (!items || items.length === 0) {
        node.className = "empty";
        node.textContent = "No usage events recorded yet.";
        return;
      }
      node.className = "";
      node.innerHTML = `<table>
        <thead><tr>
          <th>#</th><th>Event</th><th>Provider</th><th>Canonical</th><th>Record</th><th>Job</th><th>Units</th><th>Details</th><th>Created (UTC)</th>
        </tr></thead>
        <tbody>${items.map(item => `
          <tr>
            <td>${escapeHtml(String(item.id || ""))}</td>
            <td>${escapeHtml(item.event_type || "")}</td>
            <td>${escapeHtml(item.provider || "")}</td>
            <td>${escapeHtml(item.canonical_video_key || "")}</td>
            <td>${escapeHtml(item.record_id ?? "")}</td>
            <td>${escapeHtml(item.job_id || "")}</td>
            <td>${escapeHtml(String(item.units || 0))}</td>
            <td>${escapeHtml(item.details || "")}</td>
            <td>${escapeHtml(item.created_at || "")}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    }

    async function refreshInstallInfo() {
      try {
        const res = await fetch("/companion/install-info");
        const data = await res.json();
        renderInstallInfo(data);
      } catch (err) {
        renderInstallInfo({
          manifest_url: "http://localhost:8787/manifest.json",
          companion_url: "http://localhost:8787/companion",
          base_url: "http://localhost:8787"
        });
      }
    }

    async function refreshHealth() {
      try {
        const res = await fetch("/health");
        const data = await res.json();
        renderHealthStatus(data);
      } catch (err) {
        renderHealthStatus({ status: "error", cache_db_ready: false, cache_dirs_ready: false });
      }
    }

    async function refreshProviderStatus() {
      try {
        const res = await fetch("/companion/provider-status");
        const data = await res.json();
        renderGeminiStatus(data.gemini || {});
        renderSubdlStatus(data.subdl || {});
        renderSubsourceStatus(data.subsource || {});
      } catch (err) {
        renderGeminiStatus({ configured: false, model: "unknown", message: "Failed to load status: " + err.message });
        renderSubdlStatus({ configured: false, base_url: "unknown", message: "Failed to load status: " + err.message });
        renderSubsourceStatus({ configured: false, base_url: "unknown", message: "Failed to load status: " + err.message });
      }
    }

    async function refreshUsage() {
      try {
        const [statusRes, eventsRes] = await Promise.all([
          fetch("/companion/usage-status"),
          fetch("/companion/usage-events?limit=12")
        ]);
        const statusData = await statusRes.json();
        const eventsData = await eventsRes.json();
        renderUsageStatus(statusData);
        renderUsageEvents(eventsData.items || []);
      } catch (err) {
        const panel = document.getElementById("usage-status-panel");
        panel.className = "status-panel msg err";
        panel.textContent = "Failed to load usage status: " + err.message;
        const events = document.getElementById("usage-events");
        events.className = "empty";
        events.textContent = "Failed to load usage events.";
      }
    }

    async function refreshSubsourceStatus() {
      try {
        const res = await fetch("/companion/subsource-status");
        const data = await res.json();
        renderSubsourceStatus(data);
      } catch (err) {
        renderSubsourceStatus({ configured: false, base_url: "unknown", message: "Failed to load SubSource status: " + err.message });
      }
    }

    function readPreparePayload() {
      const params = readFormValues("prepare-form");
      return {
        video_id: params.get("video_id") || "",
        video_type: params.get("video_type") || "movie",
        season: params.get("season") || null,
        episode: params.get("episode") || null,
        query: params.get("query") || null,
        release_name: params.get("release_name") || null,
        language: "en",
        force: document.getElementById("prepare_force").checked
      };
    }

    function formatProgress(record) {
      if (record.progress_message) {
        if (record.progress_total_chunks !== null && record.progress_done_chunks !== null) {
          return record.progress_done_chunks + "/" + record.progress_total_chunks + " - " + record.progress_message;
        }
        return record.progress_message;
      }
      if (record.progress_total_chunks !== null && record.progress_done_chunks !== null) {
        return record.progress_done_chunks + "/" + record.progress_total_chunks;
      }
      return "";
    }

    async function fetchTranslationStatus(recordId) {
      const res = await fetch("/companion/translation-status/" + recordId);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Failed to load translation status");
      }
      return data;
    }

    async function fetchJobStatus(jobId) {
      const res = await fetch("/companion/job-status/" + jobId);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Failed to load job status");
      }
      return data;
    }

    function stopJobPolling(jobId) {
      const poller = window._jobPollers[jobId];
      if (poller) {
        clearInterval(poller);
        delete window._jobPollers[jobId];
      }
    }

    function startJobPolling(jobId, recordId) {
      stopJobPolling(jobId);
      const tick = async () => {
        try {
          const status = await fetchJobStatus(jobId);
          const msg = document.getElementById("row-msg-" + recordId);
          const progress = formatProgress(status);
          if (msg) {
            const pieces = [];
            pieces.push("Job: " + (status.status || "unknown"));
            if (progress) pieces.push(progress);
            if (status.error_message && status.status === "failed") pieces.push(status.error_message);
            msg.innerHTML = "<span class='msg " +
              (status.status === "failed" ? "err" : "note") +
              "'>" + escapeHtml(pieces.join(" | ")) + "</span>";
          }
          if (status.status === "completed" || status.status === "failed") {
            stopJobPolling(jobId);
            await refreshList();
            await refreshUsage();
          }
        } catch (err) {
          stopJobPolling(jobId);
        }
      };
      tick();
      window._jobPollers[jobId] = setInterval(tick, 2000);
    }

    async function translateRecord(recordId, btn, force) {
      const msg = document.getElementById("row-msg-" + recordId);
      if (!geminiStatus.configured) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(geminiStatus.message) + "</span>";
        return;
      }

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Translating...";
      if (msg) msg.innerHTML = "";
      let poller = null;
      try {
        poller = setInterval(async () => {
          try {
            const status = await fetchTranslationStatus(recordId);
            const progress = formatProgress(status);
            if (msg) {
              msg.innerHTML = progress
                ? "<span class='msg note'>" + escapeHtml(progress) + "</span>"
                : "";
            }
          } catch (ignored) {
          }
        }, 700);
        const suffix = force ? "?force=true" : "";
        const res = await fetch("/companion/translate/" + recordId + suffix, { method: "POST" });
        const data = await res.json();
        if (data.status === "limit_exceeded") {
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.message || "Daily limit exceeded") + "</span>";
          await refreshUsage();
          return;
        }
        if (!res.ok && msg) {
          msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.detail || "Translate failed") + "</span>";
        }
        await refreshList();
        await refreshUsage();
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
      } finally {
        if (poller) clearInterval(poller);
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    window._translateRecord = translateRecord;

    async function translateBackground(recordId, btn, force) {
      const msg = document.getElementById("row-msg-" + recordId);
      if (!geminiStatus.configured) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(geminiStatus.message) + "</span>";
        return;
      }

      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Queueing...";
      try {
        const suffix = force ? "?force=true" : "";
        const res = await fetch("/companion/translate-background/" + recordId + suffix, { method: "POST" });
        const data = await res.json();
        if (data.status === "limit_exceeded") {
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.message || "Daily limit exceeded") + "</span>";
          await refreshUsage();
          return;
        }
        if (!res.ok) {
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.detail || "Background translate failed") + "</span>";
          return;
        }
        if (data.status === "already_translated") {
          if (msg) msg.innerHTML = "<span class='msg note'>Arabic subtitle already exists.</span>";
          await refreshList();
          return;
        }
        if (msg) msg.innerHTML = "<span class='msg note'>Background job " + escapeHtml(data.job_id || "") + " started.</span>";
        await refreshList();
        await refreshUsage();
        if (data.job_id) {
          startJobPolling(data.job_id, recordId);
        }
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    window._translateBackground = translateBackground;

    async function importProviderResult(itemsName, index, status, resultElementId, endpoint, videoIdInputId, videoTypeInputId, seasonInputId, episodeInputId, releaseNameInputId, btn) {
      const box = document.getElementById(resultElementId);
      if (!status.configured) {
        box.innerHTML = "<div class='msg err'>" + escapeHtml(status.message) + "</div>";
        return;
      }

      const item = window[itemsName][index];
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Importing...";
      try {
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            video_id: document.getElementById(videoIdInputId).value,
            video_type: document.getElementById(videoTypeInputId).value,
            season: document.getElementById(seasonInputId).value || null,
            episode: document.getElementById(episodeInputId).value || null,
            release_name: item.release_name || document.getElementById(releaseNameInputId).value || null,
            subtitle_id: item.subtitle_id || null,
            download_url: item.download_url
          })
        });
        const data = await res.json();
        if (!res.ok) {
          box.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Import failed") + "</div>";
        } else {
          box.innerHTML = "<div class='msg ok'>Imported as record #" + escapeHtml(data.id) + ".</div>";
          await refreshList();
          await refreshUsage();
        }
      } catch (err) {
        box.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }

    async function importSubdlResult(index, btn) {
      return importProviderResult(
        "_subdlItems",
        index,
        subdlStatus,
        "subdl-result",
        "/companion/import-subdl",
        "subdl_video_id",
        "subdl_video_type",
        "subdl_season",
        "subdl_episode",
        "subdl_release_name",
        btn
      );
    }
    window._importSubdlResult = importSubdlResult;

    async function importSubsourceResult(index, btn) {
      return importProviderResult(
        "_subsourceItems",
        index,
        subsourceStatus,
        "subsource-result",
        "/companion/import-subsource",
        "subsource_video_id",
        "subsource_video_type",
        "subsource_season",
        "subsource_episode",
        "subsource_release_name",
        btn
      );
    }
    window._importSubsourceResult = importSubsourceResult;

    function fillTimingForm(recordId, offsetMs, target) {
      document.getElementById("timing_record_id").value = String(recordId);
      document.getElementById("timing_offset_ms").value = String(offsetMs);
      document.getElementById("timing_target").value = target;
    }
    window._fillTimingForm = fillTimingForm;

    async function previewRecord(recordId, lang) {
      const node = document.getElementById("preview-result");
      node.className = "";
      node.innerHTML = "Loading preview...";
      try {
        const res = await fetch("/companion/preview/" + recordId + "?lang=" + encodeURIComponent(lang));
        const data = await res.json();
        if (!res.ok) {
          node.className = "msg err";
          node.textContent = data.detail || "Preview failed";
          return;
        }
        if (!data.available) {
          node.className = "msg note";
          node.textContent = "No " + lang + " subtitle file is available for record #" + recordId + ".";
          return;
        }
        const blocks = (data.preview_blocks || []).map(block =>
          "<tr><td>" + escapeHtml(String(block.index)) + "</td>" +
          "<td>" + escapeHtml(block.timestamp || "") + "</td>" +
          "<td><pre style='white-space:pre-wrap;margin:0;'>" + escapeHtml(block.text || "") + "</pre></td></tr>"
        ).join("");
        node.innerHTML = "<div class='msg note'>Previewing " + escapeHtml(lang) + " for record #" + escapeHtml(String(recordId)) + ".</div>" +
          "<table><thead><tr><th>#</th><th>Timestamp</th><th>Text</th></tr></thead><tbody>" + blocks + "</tbody></table>";
      } catch (err) {
        node.className = "msg err";
        node.textContent = err.message;
      }
    }
    window._previewRecord = previewRecord;

    async function setPreferredRecord(recordId, btn) {
      const msg = document.getElementById("row-msg-" + recordId);
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Saving...";
      try {
        const res = await fetch("/companion/set-preferred/" + recordId, { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.detail || "Set preferred failed") + "</span>";
          return;
        }
        if (msg) msg.innerHTML = "<span class='msg ok'>Preferred record saved.</span>";
        await refreshList();
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    window._setPreferredRecord = setPreferredRecord;

    async function saveRecordNote(recordId, currentNote) {
      const nextNote = window.prompt("Update note for record #" + recordId, currentNote || "");
      if (nextNote === null) return;
      const msg = document.getElementById("row-msg-" + recordId);
      try {
        const res = await fetch("/companion/update-note/" + recordId, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_note: nextNote })
        });
        const data = await res.json();
        if (!res.ok) {
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.detail || "Update note failed") + "</span>";
          return;
        }
        if (msg) msg.innerHTML = "<span class='msg ok'>Note saved.</span>";
        await refreshList();
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
      }
    }
    window._saveRecordNote = saveRecordNote;

    async function adjustRecordTiming(recordId, offsetMs, target, force, btn) {
      const msg = document.getElementById("row-msg-" + recordId);
      fillTimingForm(recordId, offsetMs, target);
      if (btn) {
        btn.disabled = true;
      }
      try {
        const res = await fetch("/companion/adjust-timing/" + recordId, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ offset_ms: offsetMs, target: target, force: !!force })
        });
        const data = await res.json();
        const timingResult = document.getElementById("timing-result");
        if (!res.ok) {
          const text = data.detail || "Adjust timing failed";
          if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(text) + "</span>";
          timingResult.innerHTML = "<div class='msg err'>" + escapeHtml(text) + "</div>";
          return;
        }
        const summary = "Adjusted " + (data.adjusted_targets || []).join(", ") +
          " by " + String(data.offset_ms) + "ms.";
        if (msg) msg.innerHTML = "<span class='msg ok'>" + escapeHtml(summary) + "</span>";
        timingResult.innerHTML = "<div class='msg ok'>" + escapeHtml(summary) + "</div>";
        await refreshList();
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
        document.getElementById("timing-result").innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        if (btn) {
          btn.disabled = false;
        }
      }
    }
    window._adjustRecordTiming = adjustRecordTiming;

    function renderProviderResults(nodeId, items, importFnName, emptyText) {
      const node = document.getElementById(nodeId);
      if (!items || items.length === 0) {
        node.className = "empty";
        node.textContent = emptyText;
        return;
      }
      node.className = "";
      node.innerHTML = `<table>
        <thead><tr>
          <th>Provider</th><th>Subtitle ID</th><th>Language</th><th>Release</th>
          <th>Score</th><th>Action</th>
        </tr></thead>
        <tbody>${items.map((item, index) => `
          <tr>
            <td>${escapeHtml(item.provider)}</td>
            <td>${escapeHtml(item.subtitle_id || "")}</td>
            <td>${escapeHtml(item.language || "")}</td>
            <td>${escapeHtml(item.release_name || "")}</td>
            <td>${escapeHtml(String(item.score || 0))}</td>
            <td><button onclick="${importFnName}(${index}, this)">Import</button></td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    }

    function renderSearchAllResults(items, providerErrors, searchedProviders) {
      const node = document.getElementById("search-all-results");
      const summaries = [];
      if (searchedProviders && searchedProviders.length) {
        summaries.push("Searched: " + searchedProviders.map(escapeHtml).join(", "));
      }
      const errorKeys = Object.keys(providerErrors || {});
      if (errorKeys.length) {
        summaries.push("Provider issues: " + errorKeys.map(key => escapeHtml(key + ": " + providerErrors[key])).join(" | "));
      }

      if (!items || items.length === 0) {
        node.className = "empty";
        node.innerHTML = escapeHtml("No ranked results found.") +
          (summaries.length ? `<div class="summary">${summaries.join("<br />")}</div>` : "");
        return;
      }

      node.className = "";
      node.innerHTML = `<table>
        <thead><tr>
          <th>Rank</th><th>Provider</th><th>Subtitle ID</th><th>Language</th><th>Release</th><th>Score</th>
        </tr></thead>
        <tbody>${items.map((item, index) => `
          <tr>
            <td>${index + 1}</td>
            <td>${escapeHtml(item.provider)}</td>
            <td>${escapeHtml(item.subtitle_id || "")}</td>
            <td>${escapeHtml(item.language || "")}</td>
            <td>${escapeHtml(item.release_name || "")}</td>
            <td>${escapeHtml(String(item.score || 0))}</td>
          </tr>`).join("")}
        </tbody>
      </table>` +
      (summaries.length ? `<div class="msg note">${summaries.join("<br />")}</div>` : "");
    }

    async function refreshList() {
      try {
        const res = await fetch("/companion/list");
        const data = await res.json();
        const list = document.getElementById("list");
        if (!data.items || data.items.length === 0) {
          list.className = "empty";
          list.textContent = "No subtitles uploaded yet.";
          return;
        }
        const rows = data.items.map(r => {
          const badgeClass = r.status === "translated"
            ? "done"
            : (r.status === "failed" ? "failed" : "pending");
          const progress = formatProgress(r);
          let action = "<button onclick='_translateRecord(" + r.id + ", this, false)'>Translate</button>" +
            "<button onclick='_translateBackground(" + r.id + ", this, false)'>Translate in Background</button>";
          if (r.status === "failed") {
            action = "<button onclick='_translateRecord(" + r.id + ", this, false)'>Retry Translate</button>" +
              "<button onclick='_translateBackground(" + r.id + ", this, false)'>Retry in Background</button>";
          } else if (r.arabic_srt_path) {
            action = "<span class='badge done'>Arabic available</span>";
          }
          if (r.status === "translated" || r.arabic_srt_path) {
            action += "<button onclick='_translateRecord(" + r.id + ", this, true)'>Force Retranslate</button>" +
              "<button onclick='_translateBackground(" + r.id + ", this, true)'>Force Background Retranslate</button>";
          } else if (r.status === "failed") {
            action += "<button onclick='_translateRecord(" + r.id + ", this, true)'>Force Retranslate</button>" +
              "<button onclick='_translateBackground(" + r.id + ", this, true)'>Force Background Retranslate</button>";
          }
          action += "<button onclick='_previewRecord(" + r.id + ", \"english\")'>Preview English</button>";
          action += "<button onclick='_previewRecord(" + r.id + ", \"arabic\")'>Preview Arabic</button>";
          action += "<button onclick='_setPreferredRecord(" + r.id + ", this)'>Set Preferred</button>";
          action += "<button onclick='_adjustRecordTiming(" + r.id + ", 500, \"arabic\", false, this)'>Adjust Arabic +500ms</button>";
          action += "<button onclick='_adjustRecordTiming(" + r.id + ", -500, \"arabic\", false, this)'>Adjust Arabic -500ms</button>";
          action += "<button onclick='_saveRecordNote(" + r.id + ", " + JSON.stringify(r.user_note || "") + ")'>Update Note</button>";
          return `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.video_id)}</td>
              <td>${escapeHtml(r.canonical_video_key || "")}</td>
              <td>${escapeHtml(r.imdb_id || "")}</td>
              <td>${r.is_preferred ? "<span class='badge done'>preferred</span>" : ""}</td>
              <td>${escapeHtml(r.video_type)}</td>
              <td>${escapeHtml(r.season ?? "")}</td>
              <td>${escapeHtml(r.episode ?? "")}</td>
              <td>${escapeHtml(r.timing_offset_ms ?? "")}</td>
              <td>${escapeHtml(r.user_note || "")}</td>
              <td>${escapeHtml(r.release_name || "")}</td>
              <td>${escapeHtml(r.source_provider || "")}</td>
              <td><span class="badge ${badgeClass}">${escapeHtml(r.status)}</span></td>
              <td>${escapeHtml(r.error_message || "")}</td>
              <td>${escapeHtml(progress)}</td>
              <td><div class="actions">${action}</div><div id="row-msg-${r.id}"></div></td>
              <td>${escapeHtml(r.created_at)}</td>
            </tr>`;
        }).join("");
        list.className = "";
        list.innerHTML = `<table>
          <thead><tr>
            <th>#</th><th>Video ID</th><th>Canonical</th><th>IMDb</th><th>Preferred</th><th>Type</th><th>Season</th><th>Episode</th><th>Offset ms</th><th>Note</th><th>Release</th><th>Source</th>
            <th>Status</th><th>Error</th><th>Progress</th><th>Action</th><th>Created (UTC)</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
      } catch (err) {
        document.getElementById("list").textContent = "Failed to load list: " + err.message;
      }
    }

    async function searchProvider(formId, status, resultId, resultsId, itemsName, endpoint, emptyText) {
      const result = document.getElementById(resultId);
      result.innerHTML = "";
      if (!status.configured) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(status.message) + "</div>";
        return;
      }

      const params = readFormValues(formId);
      try {
        const res = await fetch(endpoint + "?" + params.toString());
        const data = await res.json();
        if (data.status === "limit_exceeded") {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.message || "Provider search limit reached") + "</div>";
          renderProviderResults(resultsId, [], "_noop", emptyText);
          await refreshUsage();
          return;
        }
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Provider search failed") + "</div>";
          renderProviderResults(resultsId, [], "_noop", emptyText);
          return;
        }
        window[itemsName] = data.items || [];
        renderProviderResults(resultsId, window[itemsName], itemsName === "_subdlItems" ? "_importSubdlResult" : "_importSubsourceResult", emptyText);
        result.innerHTML = "<div class='msg ok'>Found " + escapeHtml(String(window[itemsName].length)) + " result(s).</div>";
        await refreshUsage();
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      }
    }

    async function searchAllProviders(btn) {
      const result = document.getElementById("search-all-result");
      result.innerHTML = "";
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Searching...";
      try {
        const params = readFormValues("search-all-form");
        const res = await fetch("/companion/search-all?" + params.toString());
        const data = await res.json();
        if (data.status === "limit_exceeded") {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.message || "Provider search limit reached") + "</div>";
          renderSearchAllResults([], {}, []);
          await refreshUsage();
          return;
        }
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Unified search failed") + "</div>";
          renderSearchAllResults([], {}, []);
          return;
        }
        window._allItems = data.items || [];
        renderSearchAllResults(window._allItems, data.provider_errors || {}, data.searched_providers || []);
        result.innerHTML = "<div class='msg ok'>Found " + escapeHtml(String(window._allItems.length)) + " ranked result(s).</div>";
        await refreshUsage();
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }

    async function startPrepare(btn) {
      const result = document.getElementById("prepare-result");
      result.innerHTML = "";
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = "Preparing...";
      try {
        const payload = readPreparePayload();
        const res = await fetch("/companion/prepare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Prepare failed") + "</div>";
          return;
        }
        const level = data.status === "limit_exceeded" ? "err" : "note";
        result.innerHTML = "<div class='msg " + level + "'>" +
          escapeHtml(data.message || data.status || "Prepare result") +
          (data.provider ? "<br />Provider: " + escapeHtml(String(data.provider)) : "") +
          (data.record_id ? "<br />Record ID: " + escapeHtml(String(data.record_id)) : "") +
          (data.job_id ? "<br />Job ID: " + escapeHtml(String(data.job_id)) : "") +
          "</div>";
        await refreshList();
        await refreshUsage();
        if (data.job_id && data.record_id) {
          startJobPolling(data.job_id, data.record_id);
        }
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }

    document.getElementById("search-all-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button[type='submit']");
      await searchAllProviders(btn);
    });

    document.getElementById("prepare-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button[type='submit']");
      await startPrepare(btn);
    });

    document.getElementById("import-best-btn").addEventListener("click", async function () {
      const result = document.getElementById("search-all-result");
      result.innerHTML = "";
      this.disabled = true;
      const original = this.textContent;
      this.textContent = "Importing...";
      try {
        const payload = readSearchAllPayload();
        const res = await fetch("/companion/import-best", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.status === "limit_exceeded") {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.message || "Daily limit exceeded") + "</div>";
          await refreshUsage();
          return;
        }
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Import best failed") + "</div>";
          return;
        }
        const translated = data.arabic_srt_path ? " translated" : "";
        const background = data.job_id ? " background job " + escapeHtml(String(data.job_id)) + "." : "";
        result.innerHTML = "<div class='msg ok'>Imported best result from " +
          escapeHtml(data.provider) + " as record #" + escapeHtml(String(data.record_id)) +
          " (" + escapeHtml(data.status) + ")" + escapeHtml(translated) + "." + background + "</div>";
        await refreshList();
        await refreshUsage();
        if (data.job_id) {
          startJobPolling(data.job_id, data.record_id);
        }
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        this.disabled = false;
        this.textContent = original;
      }
    });

    document.getElementById("subdl-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      await searchProvider(
        "subdl-form",
        subdlStatus,
        "subdl-result",
        "subdl-results",
        "_subdlItems",
        "/companion/search-subdl",
        "No SubDL results found."
      );
    });

    document.getElementById("subsource-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      await searchProvider(
        "subsource-form",
        subsourceStatus,
        "subsource-result",
        "subsource-results",
        "_subsourceItems",
        "/companion/search-subsource",
        "No SubSource results found."
      );
    });

    document.getElementById("upload-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const result = document.getElementById("result");
      result.innerHTML = "";
      try {
        const res = await fetch("/companion/upload-srt", { method: "POST", body: formData });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Upload failed") + "</div>";
        } else {
          result.innerHTML = "<div class='msg ok'>Uploaded as record #" + escapeHtml(data.id) + " (status: " + escapeHtml(data.status) + ").</div>";
          e.target.reset();
          await refreshList();
          await refreshUsage();
        }
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      }
    });

    document.getElementById("timing-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const recordId = document.getElementById("timing_record_id").value;
      const offsetMs = document.getElementById("timing_offset_ms").value;
      const target = document.getElementById("timing_target").value;
      const force = document.getElementById("timing_force").checked;
      await adjustRecordTiming(Number(recordId), Number(offsetMs), target, force, null);
    });

    document.getElementById("clear-usage-events-btn").addEventListener("click", async function () {
      const result = document.getElementById("usage-clear-result");
      result.innerHTML = "";
      this.disabled = true;
      const original = this.textContent;
      this.textContent = "Clearing...";
      try {
        const res = await fetch("/companion/clear-usage-events", { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Clear failed") + "</div>";
          return;
        }
        result.innerHTML = "<div class='msg ok'>Deleted " + escapeHtml(String(data.deleted_count || 0)) + " usage event(s).</div>";
        await refreshUsage();
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      } finally {
        this.disabled = false;
        this.textContent = original;
      }
    });

    refreshInstallInfo();
    refreshHealth();
    refreshProviderStatus();
    refreshSubsourceStatus();
    refreshUsage();
    refreshList();
  </script>
</body>
</html>
"""


_SAFE_VIDEO_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _cache_dirs_ready() -> bool:
    try:
        config.ENGLISH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        config.ARABIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return config.ENGLISH_CACHE_DIR.exists() and config.ARABIC_CACHE_DIR.exists()
    except OSError:
        return False


def _cache_db_ready() -> bool:
    try:
        init_db(config.DB_PATH)
        return config.DB_PATH.exists()
    except OSError:
        return False


def _install_info(request: Request) -> Dict[str, str]:
    base_url = config.get_base_url(request)
    return {
        "manifest_url": f"{base_url}/manifest.json",
        "companion_url": f"{base_url}/companion",
        "base_url": base_url,
        "addon_name": config.ADDON_NAME,
        "version": config.ADDON_VERSION,
    }


@router.get("/companion", response_class=HTMLResponse)
def companion_page() -> HTMLResponse:
    """Serve the companion HTML page."""
    return HTMLResponse(_COMPANION_HTML)


def _slug_video_id(video_id: str) -> str:
    """Sanitize a video_id so it's safe to use in a filename."""
    return _SAFE_VIDEO_ID.sub("_", video_id.strip()) or "unknown"


def _resolve_video_identity(
    video_id: str,
    *,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, Any]:
    identity = parse_stremio_video_id(video_id, season=season, episode=episode)
    identity["raw_video_id"] = str(video_id or "").strip()
    return identity


def _store_english_srt_record(
    *,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    release_name: Optional[str],
    text: str,
    source_provider: Optional[str] = None,
    source_subtitle_id: Optional[str] = None,
    source_download_url: Optional[str] = None,
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
    identity = _resolve_video_identity(
        normalized_video_id,
        season=season,
        episode=episode,
    )
    normalized_release_name = release_name.strip() if release_name else None
    normalized_source_provider = (
        source_provider.strip().lower() if source_provider else None
    )
    normalized_source_subtitle_id = (
        str(source_subtitle_id).strip() if source_subtitle_id not in (None, "") else None
    )
    normalized_source_download_url = (
        str(source_download_url).strip()
        if source_download_url not in (None, "")
        else None
    )
    record_id = insert_subtitle(
        config.DB_PATH,
        video_id=normalized_video_id,
        imdb_id=identity["imdb_id"] or None,
        season=identity["season"],
        episode=identity["episode"],
        canonical_video_key=identity["canonical_video_key"] or None,
        video_type=normalized_video_type,
        release_name=normalized_release_name,
        english_srt_path=str(target),
        english_srt_hash=english_hash,
        arabic_srt_path=None,
        status="uploaded",
        source_provider=normalized_source_provider,
        source_subtitle_id=normalized_source_subtitle_id,
        source_download_url=normalized_source_download_url,
    )

    return {
        "id": record_id,
        "video_id": normalized_video_id,
        "imdb_id": identity["imdb_id"] or None,
        "season": identity["season"],
        "episode": identity["episode"],
        "canonical_video_key": identity["canonical_video_key"] or None,
        "is_episode": identity["is_episode"],
        "video_type": normalized_video_type,
        "release_name": normalized_release_name,
        "english_srt_path": str(target),
        "english_srt_hash": english_hash,
        "arabic_srt_path": None,
        "timing_offset_ms": None,
        "user_note": None,
        "is_preferred": 0,
        "status": "uploaded",
        "error_message": None,
        "source_provider": normalized_source_provider,
        "source_subtitle_id": normalized_source_subtitle_id,
        "source_download_url": normalized_source_download_url,
        "progress_total_chunks": None,
        "progress_done_chunks": None,
        "progress_message": None,
    }


def _parse_import_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize provider import payload fields."""
    return {
        "video_id": str(payload.get("video_id") or "").strip(),
        "video_type": str(payload.get("video_type") or "movie").strip() or "movie",
        "season": _parse_optional_int(payload.get("season")),
        "episode": _parse_optional_int(payload.get("episode")),
        "release_name": (
            str(payload.get("release_name")).strip()
            if payload.get("release_name") not in (None, "")
            else None
        ),
        "subtitle_id": (
            str(payload.get("subtitle_id")).strip()
            if payload.get("subtitle_id") not in (None, "")
            else None
        ),
        "download_url": str(payload.get("download_url") or "").strip(),
    }


def _parse_import_best_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize import-best body or form payload."""
    return {
        "video_id": str(payload.get("video_id") or "").strip(),
        "video_type": str(payload.get("video_type") or "movie").strip() or "movie",
        "season": _parse_optional_int(payload.get("season")),
        "episode": _parse_optional_int(payload.get("episode")),
        "query": _parse_optional_text(payload.get("query")),
        "language": str(payload.get("language") or "en").strip() or "en",
        "release_name": _parse_optional_text(payload.get("release_name")),
        "auto_translate": _parse_bool(payload.get("auto_translate"), default=False),
        "force_translate": _parse_bool(payload.get("force_translate"), default=False),
        "background_translate": _parse_bool(
            payload.get("background_translate"),
            default=False,
        ),
    }


def _parse_prepare_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize prepare body or form payload."""
    return {
        "video_id": str(payload.get("video_id") or "").strip(),
        "video_type": str(payload.get("video_type") or "movie").strip() or "movie",
        "season": _parse_optional_int(payload.get("season")),
        "episode": _parse_optional_int(payload.get("episode")),
        "query": _parse_optional_text(payload.get("query")),
        "language": str(payload.get("language") or "en").strip() or "en",
        "release_name": _parse_optional_text(payload.get("release_name")),
        "force": _parse_bool(payload.get("force"), default=False),
    }


async def _read_request_payload(request: Request) -> Dict[str, Any]:
    """Accept either JSON or form-encoded request bodies."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form)


def _parse_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="season and episode must be integers") from exc


def _parse_optional_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise HTTPException(status_code=400, detail="Invalid boolean value")


def _parse_lang(value: Any, *, default: str = "english") -> str:
    text = str(value or default).strip().lower() or default
    if text not in ("english", "arabic"):
        raise HTTPException(status_code=400, detail="lang must be english or arabic")
    return text


def _parse_target(value: Any, *, default: str = "arabic") -> str:
    text = str(value or default).strip().lower() or default
    if text not in ("english", "arabic", "both"):
        raise HTTPException(status_code=400, detail="target must be english, arabic, or both")
    return text


def _parse_limit(value: Any, *, default: int = 20) -> int:
    if value in (None, ""):
        return default
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="limit must be a positive integer") from exc
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be a positive integer")
    return limit


def _parse_offset_ms(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="offset_ms is required and must be an integer") from exc


def _parse_adjust_timing_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "offset_ms": _parse_offset_ms(payload.get("offset_ms")),
        "target": _parse_target(payload.get("target"), default="arabic"),
        "force": _parse_bool(payload.get("force"), default=False),
    }


def _normalize_user_note(value: Any) -> Optional[str]:
    note = _parse_optional_text(value)
    if note is None:
        return None
    if len(note) > 200:
        raise HTTPException(status_code=400, detail="user_note must be 200 characters or fewer")
    return note


def _record_file_path(record: Dict[str, Any], lang: str) -> Optional[Path]:
    field = "arabic_srt_path" if lang == "arabic" else "english_srt_path"
    value = _parse_optional_text(record.get(field))
    return Path(value) if value else None


def _load_srt_entries(path: Path) -> List[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        entries = parse_srt(text)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Subtitle file is missing on disk") from exc
    except SRTParseError as exc:
        raise HTTPException(status_code=400, detail="Stored subtitle file is invalid SRT") from exc
    return [
        {
            "index": entry.index,
            "timestamp": entry.timestamp,
            "text": entry.text,
        }
        for entry in entries
    ]


def _preview_record(record: Dict[str, Any], *, lang: str, limit: int) -> Dict[str, Any]:
    path = _record_file_path(record, lang)
    if not path or not path.exists():
        return {
            "record_id": int(record["id"]),
            "lang": lang,
            "available": False,
            "preview_blocks": [],
        }
    return {
        "record_id": int(record["id"]),
        "lang": lang,
        "available": True,
        "preview_blocks": _load_srt_entries(path)[:limit],
    }


def _unique_variant_path(path: Path, suffix_tag: str) -> Path:
    candidate = path.with_name("{0}.{1}{2}".format(path.stem, suffix_tag, path.suffix))
    if not candidate.exists():
        return candidate
    for counter in range(2, 1000):
        candidate = path.with_name(
            "{0}.{1}.{2}{3}".format(path.stem, suffix_tag, counter, path.suffix)
        )
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate a safe subtitle filename")


def _adjust_file_path(path: Path, *, offset_ms: int, force: bool) -> Path:
    if force:
        return path
    sign = "plus" if offset_ms >= 0 else "minus"
    return _unique_variant_path(path, "{0}{1}ms".format(sign, abs(offset_ms)))


def _backup_existing_file(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    backup_path = _unique_variant_path(path, "bak")
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path


def _write_shifted_srt(path: Path, *, offset_ms: int, force: bool) -> Dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail="Subtitle file is missing on disk")
    try:
        original_text = path.read_text(encoding="utf-8")
        shifted_text = shift_srt_content(original_text, offset_ms)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Subtitle file is missing on disk") from exc
    except SRTTimingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    destination = _adjust_file_path(path, offset_ms=offset_ms, force=force)
    backup_path = _backup_existing_file(path) if force else None
    destination.write_text(shifted_text, encoding="utf-8")
    return {
        "path": str(destination),
        "backup_path": str(backup_path) if backup_path else None,
    }


def _record_has_translated_arabic(record: Dict[str, Any]) -> bool:
    arabic_path = _record_file_path(record, "arabic")
    return bool(
        record.get("status") == "translated"
        and arabic_path
        and arabic_path.exists()
    )


def _translation_json_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record.get("id"),
        "video_id": record.get("video_id"),
        "video_type": record.get("video_type"),
        "status": record.get("status"),
        "arabic_srt_path": record.get("arabic_srt_path"),
        "error_message": record.get("error_message"),
        "progress_total_chunks": record.get("progress_total_chunks"),
        "progress_done_chunks": record.get("progress_done_chunks"),
        "progress_message": record.get("progress_message"),
    }


def _record_canonical_key(record: Dict[str, Any]) -> Optional[str]:
    return _parse_optional_text(record.get("canonical_video_key"))


def _usage_limit_payload(limit_name: str) -> Optional[Dict[str, Any]]:
    return usage_guard.check_limit(config.DB_PATH, limit_name=limit_name)


def _validate_translation_record_or_raise(record_id: int) -> Dict[str, Any]:
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")

    english_path = _record_file_path(record, "english")
    if not english_path or not english_path.exists():
        _translate_record_or_raise(record_id, force=False)
    else:
        try:
            parse_srt(english_path.read_text(encoding="utf-8"))
        except (OSError, SRTParseError):
            _translate_record_or_raise(record_id, force=False)
    return get_record(config.DB_PATH, record_id) or record


def _ensure_provider_search_allowed() -> Optional[JSONResponse]:
    limit = _usage_limit_payload(usage_guard.LIMIT_PROVIDER_SEARCHES)
    if limit:
        return JSONResponse(limit)
    return None


def _record_provider_search_event(
    *,
    provider: Optional[str] = None,
    canonical_video_key: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    usage_guard.record_event(
        config.DB_PATH,
        event_type=usage_guard.EVENT_PROVIDER_SEARCH,
        provider=provider,
        canonical_video_key=canonical_video_key,
        details=details,
    )


def _record_provider_import_event(
    *,
    provider: Optional[str],
    canonical_video_key: Optional[str],
    record_id: int,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    usage_guard.record_event(
        config.DB_PATH,
        event_type=usage_guard.EVENT_PROVIDER_IMPORT,
        provider=provider,
        canonical_video_key=canonical_video_key,
        record_id=record_id,
        details=details,
    )


def _import_provider_item(
    *,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    release_name: Optional[str],
    provider: str,
    subtitle_id: Optional[str],
    download_url: str,
) -> Dict[str, Any]:
    try:
        raw = provider_router.download_subtitle_data(provider, download_url)
        text = validate_srt_content(raw)
    except (SubDLNotConfiguredError, SubSourceNotConfiguredError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SRTValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SubDLError, SubSourceError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    stored = _store_english_srt_record(
        video_id=video_id,
        video_type=video_type,
        season=season,
        episode=episode,
        release_name=release_name,
        text=text,
        source_provider=provider,
        source_subtitle_id=subtitle_id,
        source_download_url=download_url,
    )
    _record_provider_import_event(
        provider=provider,
        canonical_video_key=_parse_optional_text(stored.get("canonical_video_key")),
        record_id=int(stored["id"]),
        details={"video_id": video_id, "video_type": video_type},
    )
    return stored


def _translate_record_or_raise(record_id: int, *, force: bool) -> Dict[str, Any]:
    try:
        return translate_record(
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
    except SRTQualityError as exc:
        raise HTTPException(
            status_code=502,
            detail="Translated Arabic SRT failed quality checks: {0}".format(exc),
        ) from exc
    except GeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TranslationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _start_background_translation(record_id: int, *, force: bool) -> Dict[str, Any]:
    """Validate a record and start or reuse a background translation job."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")

    gemini_status = get_gemini_status()
    if not gemini_status.get("configured"):
        raise HTTPException(
            status_code=400,
            detail=str(gemini_status.get("message") or "Gemini is not configured."),
        )

    arabic_path = _parse_optional_text(record.get("arabic_srt_path"))
    if (
        not force
        and record.get("status") == "translated"
        and arabic_path
        and Path(arabic_path).exists()
    ):
        return {
            "job_id": None,
            "record_id": record_id,
            "status": "already_translated",
            "error_message": None,
        }

    running_job = job_manager.get_running_job_for_record(record_id)
    if running_job:
        usage_guard.record_event(
            config.DB_PATH,
            event_type=usage_guard.EVENT_DUPLICATE_JOB_REUSED,
            canonical_video_key=_record_canonical_key(record),
            record_id=record_id,
            job_id=str(running_job.get("job_id") or ""),
            details={"source": "translate-background"},
        )
        return running_job

    _validate_translation_record_or_raise(record_id)
    limit = _usage_limit_payload(usage_guard.LIMIT_GEMINI_TRANSLATIONS)
    if limit:
        return limit

    job = job_manager.start_translation_job(
        record_id=record_id,
        force=force,
        db_path=config.DB_PATH,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )
    usage_guard.record_event(
        config.DB_PATH,
        event_type=usage_guard.EVENT_GEMINI_TRANSLATE_BACKGROUND,
        canonical_video_key=_record_canonical_key(record),
        record_id=record_id,
        job_id=str(job.get("job_id") or ""),
        details={"force": bool(force)},
    )
    return job


@router.post("/companion/upload-srt")
async def upload_srt(
    video_id: str = Form(..., description="e.g. tt1234567"),
    video_type: str = Form("movie"),
    season: Optional[int] = Form(None),
    episode: Optional[int] = Form(None),
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
            season=season,
            episode=episode,
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


@router.get("/companion/subdl-status")
def subdl_status() -> Dict[str, Any]:
    """Return whether SubDL search/import is currently configured."""
    return subdl_service.get_status()


@router.get("/companion/subsource-status")
def subsource_status() -> Dict[str, Any]:
    """Return whether SubSource search/import is currently configured."""
    return subsource_service.get_status()


@router.get("/companion/provider-status")
def provider_status() -> Dict[str, Any]:
    """Return combined Gemini, SubDL, and SubSource configuration status."""
    return provider_router.get_provider_status()


@router.get("/companion/install-info")
def install_info(request: Request) -> Dict[str, str]:
    """Return the local URLs needed to install and use the addon."""
    return _install_info(request)


@router.post("/companion/prepare")
async def prepare_endpoint(request: Request) -> JSONResponse:
    """Search, import, and background-translate the best subtitle in one action."""
    payload = _parse_prepare_payload(await _read_request_payload(request))
    if not payload["video_id"]:
        raise HTTPException(status_code=400, detail="video_id is required")

    result = prepare_service.request_prepare(
        video_id=payload["video_id"],
        video_type=payload["video_type"],
        season=payload["season"],
        episode=payload["episode"],
        query=payload["query"],
        release_name=payload["release_name"],
        language=payload["language"],
        force=bool(payload["force"]),
        db_path=config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        run_async=False,
    )
    return JSONResponse(result)


@router.get("/companion/prepare-status/{canonical_video_key}")
def prepare_status(canonical_video_key: str) -> Dict[str, Any]:
    """Return readiness, latest record, and active prepare/translation job details."""
    return prepare_service.get_prepare_status(
        canonical_video_key=str(canonical_video_key or "").strip(),
        db_path=config.DB_PATH,
    )


@router.get("/companion/usage-status")
def usage_status() -> Dict[str, Any]:
    """Return today's local usage counters and remaining daily limits."""
    return usage_guard.get_usage_status(
        config.DB_PATH,
        auto_prepare_enabled=config.is_auto_prepare_on_subtitles_request_enabled(),
    )


@router.get("/companion/usage-events")
def usage_events(
    limit: int = Query(50),
    event_type: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Return latest usage events for local quota diagnostics."""
    return {
        "items": usage_guard.list_events(
            config.DB_PATH,
            limit=_parse_limit(limit, default=50),
            event_type=_parse_optional_text(event_type),
            provider=_parse_optional_text(provider),
        )
    }


@router.post("/companion/clear-usage-events")
def clear_usage_events() -> JSONResponse:
    """Delete usage events only and return the deleted row count."""
    deleted = usage_guard.clear_events(config.DB_PATH)
    return JSONResponse({"status": "cleared", "deleted_count": deleted})


@router.get("/companion/translation-status/{record_id}")
def translation_status(record_id: int) -> Dict[str, Any]:
    """Return translation progress and availability for one subtitle record."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")
    arabic_path = _parse_optional_text(record.get("arabic_srt_path"))
    return {
        "record_id": record_id,
        "status": record.get("status"),
        "error_message": record.get("error_message"),
        "progress_total_chunks": record.get("progress_total_chunks"),
        "progress_done_chunks": record.get("progress_done_chunks"),
        "progress_message": record.get("progress_message"),
        "arabic_available": bool(arabic_path and Path(arabic_path).exists()),
    }


@router.get("/companion/preview/{record_id}")
def preview_record(
    record_id: int,
    lang: str = Query("english"),
    limit: int = Query(20),
) -> Dict[str, Any]:
    """Preview the first subtitle cues for one record and language."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")
    return _preview_record(
        record,
        lang=_parse_lang(lang),
        limit=_parse_limit(limit),
    )


@router.post("/companion/update-note/{record_id}")
async def update_note(record_id: int, request: Request) -> JSONResponse:
    """Store a short user note for one subtitle record."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")

    payload = await _read_request_payload(request)
    user_note = _normalize_user_note(payload.get("user_note"))
    set_user_note(config.DB_PATH, record_id, user_note)
    updated = get_record(config.DB_PATH, record_id) or record
    return JSONResponse(
        {
            "record_id": record_id,
            "user_note": updated.get("user_note"),
        }
    )


@router.post("/companion/adjust-timing/{record_id}")
async def adjust_timing(record_id: int, request: Request) -> JSONResponse:
    """Adjust English and/or Arabic subtitle timing for one record."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")

    payload = _parse_adjust_timing_payload(await _read_request_payload(request))
    offset_ms = int(payload["offset_ms"])
    target = str(payload["target"])
    force = bool(payload["force"])

    english_path = _record_file_path(record, "english")
    arabic_path = _record_file_path(record, "arabic")
    has_arabic = bool(arabic_path and arabic_path.exists())

    if target == "arabic" and not has_arabic:
        raise HTTPException(status_code=400, detail="Arabic subtitle is not available for this record")

    updates: Dict[str, Any] = {}
    adjusted_targets: List[str] = []
    backup_paths: Dict[str, str] = {}

    if target in ("english", "both"):
        if not english_path or not english_path.exists():
            raise HTTPException(status_code=404, detail="English subtitle file is missing on disk")
        english_result = _write_shifted_srt(english_path, offset_ms=offset_ms, force=force)
        updates["english_srt_path"] = english_result["path"]
        adjusted_targets.append("english")
        if english_result["backup_path"]:
            backup_paths["english"] = str(english_result["backup_path"])

    if target in ("arabic", "both") and has_arabic and arabic_path:
        arabic_result = _write_shifted_srt(arabic_path, offset_ms=offset_ms, force=force)
        updates["arabic_srt_path"] = arabic_result["path"]
        adjusted_targets.append("arabic")
        if arabic_result["backup_path"]:
            backup_paths["arabic"] = str(arabic_result["backup_path"])

    current_offset = int(record.get("timing_offset_ms") or 0)
    updates["timing_offset_ms"] = current_offset + offset_ms
    if not has_arabic:
        updates["status"] = "uploaded"
        updates["error_message"] = None

    update_record_media(
        config.DB_PATH,
        record_id,
        english_srt_path=updates.get("english_srt_path"),
        arabic_srt_path=updates.get("arabic_srt_path"),
        timing_offset_ms=updates.get("timing_offset_ms"),
        status=updates.get("status"),
        error_message=updates.get("error_message"),
        clear_error_message="status" in updates,
    )
    updated = get_record(config.DB_PATH, record_id) or record
    return JSONResponse(
        {
            "record_id": record_id,
            "offset_ms": offset_ms,
            "target": target,
            "force": force,
            "adjusted_targets": adjusted_targets,
            "backup_paths": backup_paths,
            "english_srt_path": updated.get("english_srt_path"),
            "arabic_srt_path": updated.get("arabic_srt_path"),
            "timing_offset_ms": updated.get("timing_offset_ms"),
            "status": updated.get("status"),
        }
    )


@router.post("/companion/set-preferred/{record_id}")
def set_preferred(record_id: int) -> JSONResponse:
    """Mark one translated Arabic record as preferred for its canonical key."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")
    if not _record_has_translated_arabic(record):
        raise HTTPException(
            status_code=400,
            detail="Only translated records with a real Arabic subtitle can be preferred",
        )

    set_preferred_record(
        config.DB_PATH,
        record_id,
        canonical_video_key=_parse_optional_text(record.get("canonical_video_key")),
        legacy_video_id=str(record.get("video_id") or ""),
    )
    updated = get_record(config.DB_PATH, record_id) or record
    return JSONResponse(
        {
            "record_id": record_id,
            "canonical_video_key": updated.get("canonical_video_key"),
            "is_preferred": bool(updated.get("is_preferred")),
        }
    )


@router.post("/companion/translate-background/{record_id}")
def translate_background(record_id: int, force: bool = Query(False)) -> JSONResponse:
    """Start a background translation job and return immediately."""
    job = _start_background_translation(record_id, force=force)
    if job.get("status") == "limit_exceeded":
        return JSONResponse(job)
    return JSONResponse(
        {
            "job_id": job.get("job_id"),
            "record_id": record_id,
            "status": job.get("status"),
        }
    )


@router.get("/companion/job-status/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    """Return the latest state for one background translation job."""
    job = job_manager.get_job_status(job_id, config.DB_PATH)
    if not job:
        raise HTTPException(status_code=404, detail=f"No background job with id={job_id}")
    return job


@router.get("/companion/diagnostics")
def diagnostics() -> Dict[str, Any]:
    """Return local readiness diagnostics for common Stremio issues."""
    python_app_import_ok = False
    try:
        import_module("backend.main")
        python_app_import_ok = True
    except Exception:
        python_app_import_ok = False

    manifest = build_manifest()
    manifest_ok = all(
        key in manifest for key in ("id", "version", "name", "resources", "types")
    )
    subtitles_route_ok = any(
        getattr(route, "path", "") == "/subtitles/{video_type}/{video_id}.json"
        for route in subtitles_router.routes
    )
    try:
        status_subtitle_ready = "-->" in build_status_srt("اختبار")
    except Exception:
        status_subtitle_ready = False
    try:
        parsed = parse_stremio_video_id("tt1234567:1:5")
        stremio_id_parser_ready = (
            parsed.get("imdb_id") == "tt1234567"
            and parsed.get("season") == 1
            and parsed.get("episode") == 5
            and parsed.get("canonical_video_key") == "tt1234567:s01e05"
            and parsed.get("is_episode") is True
        )
    except Exception:
        stremio_id_parser_ready = False
    try:
        shifted = shift_srt_content(
            "1\n00:00:01,000 --> 00:00:02,000\nTest\n",
            500,
        )
        srt_timing_ready = "00:00:01,500 --> 00:00:02,500" in shifted
    except Exception:
        srt_timing_ready = False
    try:
        columns = set(get_table_columns(config.DB_PATH))
        preferred_record_ready = all(
            column in columns
            for column in ("timing_offset_ms", "user_note", "is_preferred")
        )
    except Exception:
        preferred_record_ready = False
    try:
        prepare_service_ready = bool(prepare_service.is_ready())
    except Exception:
        prepare_service_ready = False
    try:
        usage_columns = set(usage_guard.get_usage_table_columns(config.DB_PATH))
        usage_guard_ready = all(
            column in usage_columns
            for column in (
                "id",
                "event_type",
                "provider",
                "canonical_video_key",
                "record_id",
                "job_id",
                "units",
                "details",
                "created_at",
            )
        )
    except Exception:
        usage_guard_ready = False
    usage_limits = usage_guard.get_daily_limits()
    usage_counts = usage_guard.get_usage_counts(config.DB_PATH)
    return {
        "python_app_import_ok": python_app_import_ok,
        "cache_db_ready": _cache_db_ready(),
        "cache_english_dir_ready": _cache_dirs_ready() and config.ENGLISH_CACHE_DIR.exists(),
        "cache_arabic_dir_ready": _cache_dirs_ready() and config.ARABIC_CACHE_DIR.exists(),
        "sample_arabic_exists": config.SAMPLE_SRT_PATH.exists(),
        "gemini_configured": bool(get_gemini_status().get("configured")),
        "subdl_configured": bool(subdl_service.get_status().get("configured")),
        "subsource_configured": bool(subsource_service.get_status().get("configured")),
        "manifest_ok": manifest_ok,
        "subtitles_route_ok": subtitles_route_ok,
        "active_translation_jobs": job_manager.active_job_count(),
        "job_manager_ready": job_manager.is_ready(),
        "status_subtitle_ready": status_subtitle_ready,
        "stremio_id_parser_ready": stremio_id_parser_ready,
        "srt_timing_ready": srt_timing_ready,
        "preferred_record_ready": preferred_record_ready,
        "prepare_service_ready": prepare_service_ready,
        "auto_prepare_enabled": config.is_auto_prepare_on_subtitles_request_enabled(),
        "usage_guard_ready": usage_guard_ready,
        "allow_auto_prepare_when_limited": config.is_allow_auto_prepare_when_limited_enabled(),
        "max_daily_gemini_translations": usage_limits[usage_guard.LIMIT_GEMINI_TRANSLATIONS],
        "max_daily_provider_searches": usage_limits[usage_guard.LIMIT_PROVIDER_SEARCHES],
        "max_daily_prepare_requests": usage_limits[usage_guard.LIMIT_PREPARE_REQUESTS],
        "today_gemini_translations_used": usage_counts["gemini_translations_used"],
        "today_provider_searches_used": usage_counts["provider_searches_used"],
        "today_prepare_requests_used": usage_counts["prepare_requests_used"],
    }


@router.post("/companion/test-gemini")
def test_gemini() -> JSONResponse:
    """Run a tiny Gemini smoke test when configured."""
    status = get_gemini_status()
    if not status.get("configured"):
        return JSONResponse(
            {
                "configured": False,
                "success": False,
                "message": status.get("message"),
            }
        )

    try:
        reply = gemini_service.generate(
            'Translate "Hello" to Arabic. Return only the Arabic word.'
        )
    except GeminiError as exc:
        return JSONResponse(
            {
                "configured": True,
                "success": False,
                "message": str(exc),
            },
            status_code=502,
        )

    return JSONResponse(
        {
            "configured": True,
            "success": True,
            "reply": str(reply).strip(),
        }
    )


@router.post("/companion/self-test")
def self_test() -> Dict[str, Any]:
    """Run a safe local self-test without calling Gemini by default."""
    db_ready = _cache_db_ready()
    dirs_ready = _cache_dirs_ready()
    temp_video_id = "selftest-{0}".format(uuid.uuid4().hex[:10])
    record_id: Optional[int] = None
    english_path: Optional[Path] = None
    cleanup_performed = False
    report: Dict[str, Any] = {
        "db_ready": db_ready,
        "cache_dirs_ready": dirs_ready,
        "created_record": False,
        "translation_status_ok": False,
        "cleanup_performed": False,
        "gemini_called": False,
    }

    try:
        payload = _store_english_srt_record(
            video_id=temp_video_id,
            video_type="movie",
            season=None,
            episode=None,
            release_name="Self.Test",
            text=(
                "1\n"
                "00:00:01,000 --> 00:00:02,000\n"
                "Self test line\n"
            ),
            source_provider="self-test",
        )
        record_id = int(payload["id"])
        english_path = Path(payload["english_srt_path"])
        report["created_record"] = get_record(config.DB_PATH, record_id) is not None

        status_payload = translation_status(record_id)
        report["translation_status_ok"] = (
            status_payload.get("record_id") == record_id
            and status_payload.get("status") == "uploaded"
            and status_payload.get("arabic_available") is False
        )
        report["translation_status"] = status_payload
    finally:
        if english_path and english_path.exists():
            english_path.unlink()
            cleanup_performed = True
        if record_id is not None:
            delete_record(config.DB_PATH, record_id)
            cleanup_performed = True
        report["cleanup_performed"] = cleanup_performed

    report["status"] = (
        "ok"
        if report["db_ready"] and report["cache_dirs_ready"] and report["created_record"] and report["translation_status_ok"]
        else "failed"
    )
    return report


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
    limit_response = _ensure_provider_search_allowed()
    if limit_response:
        return limit_response
    identity = _resolve_video_identity(video_id, season=season, episode=episode)
    _record_provider_search_event(
        provider=PROVIDER_SUBDL,
        canonical_video_key=_parse_optional_text(identity.get("canonical_video_key")),
        details={"video_id": video_id, "video_type": video_type},
    )
    try:
        items = subdl_service.search_subtitles(
            video_id=identity["imdb_id"] or video_id,
            video_type=video_type,
            season=identity["season"],
            episode=identity["episode"],
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
    limit_response = _ensure_provider_search_allowed()
    if limit_response:
        return limit_response
    identity = _resolve_video_identity(video_id, season=season, episode=episode)
    _record_provider_search_event(
        provider=PROVIDER_SUBSOURCE,
        canonical_video_key=_parse_optional_text(identity.get("canonical_video_key")),
        details={"video_id": video_id, "video_type": video_type},
    )
    try:
        items = subsource_service.search_subtitles(
            video_id=identity["imdb_id"] or video_id,
            video_type=video_type,
            season=identity["season"],
            episode=identity["episode"],
            query=query,
            language=language,
            release_name=release_name,
        )
    except SubSourceNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": items}


@router.get("/companion/search-all")
def search_all(
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    query: Optional[str] = Query(None),
    language: str = Query("en"),
    release_name: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Search all configured providers and return ranked combined results."""
    if not video_id or not video_id.strip():
        raise HTTPException(status_code=400, detail="video_id is required")
    limit_response = _ensure_provider_search_allowed()
    if limit_response:
        return limit_response
    identity = _resolve_video_identity(video_id, season=season, episode=episode)
    _record_provider_search_event(
        provider="all",
        canonical_video_key=_parse_optional_text(identity.get("canonical_video_key")),
        details={"video_id": video_id, "video_type": video_type},
    )

    return provider_router.search_all_subtitles(
        video_id=identity["imdb_id"] or video_id,
        video_type=video_type,
        season=identity["season"],
        episode=identity["episode"],
        query=query,
        language=language,
        release_name=release_name,
    )


@router.post("/companion/import-subdl")
async def import_subdl(request: Request) -> JSONResponse:
    """Download an English SRT from SubDL, validate it, and add a DB record."""
    payload = _parse_import_payload(await _read_request_payload(request))
    if not payload["video_id"]:
        raise HTTPException(status_code=400, detail="video_id is required")
    if not payload["download_url"]:
        raise HTTPException(status_code=400, detail="download_url is required")

    return JSONResponse(
        _import_provider_item(
            video_id=str(payload["video_id"]),
            video_type=str(payload["video_type"]),
            season=payload["season"],
            episode=payload["episode"],
            release_name=payload["release_name"],
            provider=PROVIDER_SUBDL,
            subtitle_id=payload["subtitle_id"],
            download_url=str(payload["download_url"]),
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

    return JSONResponse(
        _import_provider_item(
            video_id=str(payload["video_id"]),
            video_type=str(payload["video_type"]),
            season=payload["season"],
            episode=payload["episode"],
            release_name=payload["release_name"],
            provider=PROVIDER_SUBSOURCE,
            subtitle_id=payload["subtitle_id"],
            download_url=str(payload["download_url"]),
        )
    )


@router.post("/companion/import-best")
async def import_best(request: Request) -> JSONResponse:
    """Search all providers, import the best match, and optionally translate it."""
    payload = _parse_import_best_payload(await _read_request_payload(request))
    if not payload["video_id"]:
        raise HTTPException(status_code=400, detail="video_id is required")
    limit_response = _ensure_provider_search_allowed()
    if limit_response:
        return limit_response
    identity = _resolve_video_identity(
        payload["video_id"],
        season=payload["season"],
        episode=payload["episode"],
    )
    _record_provider_search_event(
        provider="all",
        canonical_video_key=_parse_optional_text(identity.get("canonical_video_key")),
        details={"video_id": payload["video_id"], "video_type": payload["video_type"]},
    )

    search_result = provider_router.search_all_subtitles(
        video_id=identity["imdb_id"] or payload["video_id"],
        video_type=payload["video_type"],
        season=identity["season"],
        episode=identity["episode"],
        query=payload["query"],
        language=payload["language"],
        release_name=payload["release_name"],
    )
    items = search_result.get("items") or []
    if not items:
        raise HTTPException(
            status_code=404,
            detail="No subtitle results found across SubDL and SubSource.",
        )

    best = dict(items[0])
    release_name = (
        _parse_optional_text(best.get("release_name")) or payload["release_name"]
    )
    stored = _import_provider_item(
        video_id=payload["video_id"],
        video_type=payload["video_type"],
        season=identity["season"],
        episode=identity["episode"],
        release_name=release_name,
        provider=str(best.get("provider") or ""),
        subtitle_id=_parse_optional_text(best.get("subtitle_id")),
        download_url=str(best.get("download_url") or ""),
    )

    status = stored["status"]
    arabic_srt_path = stored["arabic_srt_path"]
    job_id = None
    if payload["auto_translate"] and get_gemini_status().get("configured"):
        if payload["background_translate"]:
            job = _start_background_translation(
                int(stored["id"]),
                force=bool(payload["force_translate"]),
            )
            status = job.get("status") or status
            job_id = job.get("job_id")
        else:
            translated = _translate_record_or_raise(
                int(stored["id"]),
                force=bool(payload["force_translate"]),
            )
            status = translated.get("status") or status
            arabic_srt_path = translated.get("arabic_srt_path")

    return JSONResponse(
        {
            "record_id": stored["id"],
            "provider": best.get("provider"),
            "score": best.get("score"),
            "status": status,
            "arabic_srt_path": arabic_srt_path,
            "job_id": job_id,
        }
    )


@router.post("/companion/translate/{record_id}")
def translate_endpoint(record_id: int, force: bool = Query(False)) -> JSONResponse:
    """Translate the English SRT for `record_id` and persist the Arabic file."""
    record = get_record(config.DB_PATH, record_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No subtitle record with id={record_id}")
    if _record_has_translated_arabic(record) and not force:
        return JSONResponse(_translation_json_payload(record))

    gemini_status = get_gemini_status()
    if not gemini_status.get("configured"):
        updated = _translate_record_or_raise(record_id, force=force)
        return JSONResponse(_translation_json_payload(updated))

    _validate_translation_record_or_raise(record_id)
    limit = _usage_limit_payload(usage_guard.LIMIT_GEMINI_TRANSLATIONS)
    if limit:
        return JSONResponse(limit)

    usage_guard.record_event(
        config.DB_PATH,
        event_type=usage_guard.EVENT_GEMINI_TRANSLATE_SYNC,
        canonical_video_key=_record_canonical_key(record),
        record_id=record_id,
        details={"force": bool(force)},
    )
    updated = _translate_record_or_raise(record_id, force=force)
    return JSONResponse(_translation_json_payload(updated))
