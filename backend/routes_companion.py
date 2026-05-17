"""Local companion: upload, provider import, unified search, and Gemini translation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from . import config
from services import provider_router, subdl_service, subsource_service
from services.cache_db import get_record, insert_subtitle, list_subtitles
from services.gemini_service import (
    GeminiError,
    GeminiNotConfiguredError,
    get_status as get_gemini_status,
)
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
from utils.srt_quality import SRTQualityError
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
  <p class="sub">Upload an English <code>.srt</code>, search providers, or import the best ranked English subtitle and optionally translate it to Arabic with Gemini.</p>

  <div id="gemini-status" class="status-panel msg">Checking Gemini configuration...</div>
  <div id="subdl-status" class="status-panel msg">Checking SubDL configuration...</div>
  <div id="subsource-status" class="status-panel msg">Checking SubSource configuration...</div>

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
        auto_translate: document.getElementById("all_auto_translate").checked
      };
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

    async function refreshSubsourceStatus() {
      try {
        const res = await fetch("/companion/subsource-status");
        const data = await res.json();
        renderSubsourceStatus(data);
      } catch (err) {
        renderSubsourceStatus({ configured: false, base_url: "unknown", message: "Failed to load SubSource status: " + err.message });
      }
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
        if (!res.ok && msg) {
          msg.innerHTML = "<span class='msg err'>" + escapeHtml(data.detail || "Translate failed") + "</span>";
        }
        await refreshList();
      } catch (err) {
        if (msg) msg.innerHTML = "<span class='msg err'>" + escapeHtml(err.message) + "</span>";
      } finally {
        if (poller) clearInterval(poller);
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    window._translateRecord = translateRecord;

    async function importProviderResult(itemsName, index, status, resultElementId, endpoint, videoIdInputId, videoTypeInputId, releaseNameInputId, btn) {
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
        "subsource_release_name",
        btn
      );
    }
    window._importSubsourceResult = importSubsourceResult;

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
          let action = "<button onclick='_translateRecord(" + r.id + ", this, false)'>Translate</button>";
          if (r.status === "failed") {
            action = "<button onclick='_translateRecord(" + r.id + ", this, false)'>Retry Translate</button>";
          } else if (r.arabic_srt_path) {
            action = "<span class='badge done'>Arabic available</span>";
          }
          if (r.status === "translated" || r.arabic_srt_path) {
            action += "<button onclick='_translateRecord(" + r.id + ", this, true)'>Force Retranslate</button>";
          } else if (r.status === "failed") {
            action += "<button onclick='_translateRecord(" + r.id + ", this, true)'>Force Retranslate</button>";
          }
          return `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.video_id)}</td>
              <td>${escapeHtml(r.video_type)}</td>
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
            <th>#</th><th>Video ID</th><th>Type</th><th>Release</th><th>Source</th>
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
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Provider search failed") + "</div>";
          renderProviderResults(resultsId, [], "_noop", emptyText);
          return;
        }
        window[itemsName] = data.items || [];
        renderProviderResults(resultsId, window[itemsName], itemsName === "_subdlItems" ? "_importSubdlResult" : "_importSubsourceResult", emptyText);
        result.innerHTML = "<div class='msg ok'>Found " + escapeHtml(String(window[itemsName].length)) + " result(s).</div>";
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
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Unified search failed") + "</div>";
          renderSearchAllResults([], {}, []);
          return;
        }
        window._allItems = data.items || [];
        renderSearchAllResults(window._allItems, data.provider_errors || {}, data.searched_providers || []);
        result.innerHTML = "<div class='msg ok'>Found " + escapeHtml(String(window._allItems.length)) + " ranked result(s).</div>";
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
        if (!res.ok) {
          result.innerHTML = "<div class='msg err'>" + escapeHtml(data.detail || "Import best failed") + "</div>";
          return;
        }
        const translated = data.arabic_srt_path ? " translated" : "";
        result.innerHTML = "<div class='msg ok'>Imported best result from " +
          escapeHtml(data.provider) + " as record #" + escapeHtml(String(data.record_id)) +
          " (" + escapeHtml(data.status) + ")" + escapeHtml(translated) + ".</div>";
        await refreshList();
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
        }
      } catch (err) {
        result.innerHTML = "<div class='msg err'>" + escapeHtml(err.message) + "</div>";
      }
    });

    refreshProviderStatus();
    refreshSubsourceStatus();
    refreshList();
  </script>
</body>
</html>
"""


_SAFE_VIDEO_ID = re.compile(r"[^A-Za-z0-9._-]+")


@router.get("/companion", response_class=HTMLResponse)
def companion_page() -> HTMLResponse:
    """Serve the companion HTML page."""
    return HTMLResponse(_COMPANION_HTML)


def _slug_video_id(video_id: str) -> str:
    """Sanitize a video_id so it's safe to use in a filename."""
    return _SAFE_VIDEO_ID.sub("_", video_id.strip()) or "unknown"


def _store_english_srt_record(
    *,
    video_id: str,
    video_type: str,
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
        "video_type": normalized_video_type,
        "release_name": normalized_release_name,
        "english_srt_path": str(target),
        "english_srt_hash": english_hash,
        "arabic_srt_path": None,
        "status": "uploaded",
        "error_message": None,
        "source_provider": normalized_source_provider,
        "source_subtitle_id": normalized_source_subtitle_id,
        "source_download_url": normalized_source_download_url,
        "progress_total_chunks": None,
        "progress_done_chunks": None,
        "progress_message": None,
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


def _import_provider_item(
    *,
    video_id: str,
    video_type: str,
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

    return _store_english_srt_record(
        video_id=video_id,
        video_type=video_type,
        release_name=release_name,
        text=text,
        source_provider=provider,
        source_subtitle_id=subtitle_id,
        source_download_url=download_url,
    )


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

    return provider_router.search_all_subtitles(
        video_id=video_id,
        video_type=video_type,
        season=season,
        episode=episode,
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

    search_result = provider_router.search_all_subtitles(
        video_id=payload["video_id"],
        video_type=payload["video_type"],
        season=payload["season"],
        episode=payload["episode"],
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
        release_name=release_name,
        provider=str(best.get("provider") or ""),
        subtitle_id=_parse_optional_text(best.get("subtitle_id")),
        download_url=str(best.get("download_url") or ""),
    )

    status = stored["status"]
    arabic_srt_path = stored["arabic_srt_path"]
    if payload["auto_translate"] and get_gemini_status().get("configured"):
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
        }
    )


@router.post("/companion/translate/{record_id}")
def translate_endpoint(record_id: int, force: bool = Query(False)) -> JSONResponse:
    """Translate the English SRT for `record_id` and persist the Arabic file."""
    updated = _translate_record_or_raise(record_id, force=force)
    return JSONResponse(
        {
            "id": updated.get("id"),
            "video_id": updated.get("video_id"),
            "video_type": updated.get("video_type"),
            "status": updated.get("status"),
            "arabic_srt_path": updated.get("arabic_srt_path"),
            "error_message": updated.get("error_message"),
            "progress_total_chunks": updated.get("progress_total_chunks"),
            "progress_done_chunks": updated.get("progress_done_chunks"),
            "progress_message": updated.get("progress_message"),
        }
    )
