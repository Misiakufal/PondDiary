import cgi
import json
import mimetypes
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


DATA_DIR = Path("/data")
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "pond_diary.db"
OPTIONS_PATH = DATA_DIR / "options.json"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8099"))
DEFAULT_OPTIONS = {
    "default_mode": "water_test",
    "black_mode": False,
}
VALID_MODES = {"water_test", "product", "photo"}


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                event_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                photo_path TEXT,
                details_json TEXT NOT NULL
            )
            """
        )
        connection.commit()


def load_options() -> dict:
    options = dict(DEFAULT_OPTIONS)
    if OPTIONS_PATH.exists():
        try:
            stored = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                options.update(stored)
        except (OSError, json.JSONDecodeError):
            pass

    default_mode = str(options.get("default_mode", DEFAULT_OPTIONS["default_mode"])).strip()
    options["default_mode"] = default_mode if default_mode in VALID_MODES else DEFAULT_OPTIONS["default_mode"]
    options["black_mode"] = bool(options.get("black_mode", False))
    return options


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_text(value: str) -> str:
    return value.strip()


def insert_entry(entry_type: str, title: str, description: str, event_date: str, details: dict, photo_path: str | None = None) -> None:
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO entries (entry_type, title, description, event_date, created_at, photo_path, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_type,
                title,
                description,
                event_date,
                utc_now(),
                photo_path,
                json.dumps(details, ensure_ascii=True),
            ),
        )
        connection.commit()


def fetch_entries() -> list[dict]:
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, entry_type, title, description, event_date, created_at, photo_path, details_json
            FROM entries
            ORDER BY event_date DESC, id DESC
            """
        ).fetchall()

    entries = []
    for row in rows:
        details = json.loads(row["details_json"])
        entries.append(
            {
                "id": row["id"],
                "type": row["entry_type"],
                "title": row["title"],
                "description": row["description"] or "",
                "eventDate": row["event_date"],
                "createdAt": row["created_at"],
                "details": details,
                "photoUrl": f"/uploads/{row['photo_path']}" if row["photo_path"] else None,
            }
        )
    return entries
def delete_entry(entry_id: int) -> bool:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT photo_path FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return False

        connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        connection.commit()

    photo_path = row["photo_path"]
    if photo_path:
        file_path = UPLOADS_DIR / photo_path
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError:
            pass
    return True


def parse_json(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise ValueError("Request body is required.")
    raw_body = handler.rfile.read(content_length)
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON payload.") from exc


def validate_event_date(value: str) -> str:
    cleaned = normalize_text(value)
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Date must use YYYY-MM-DD.") from exc
    return cleaned


def json_response(handler: BaseHTTPRequestHandler, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, body: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/html; charset=utf-8") -> None:
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def save_uploaded_photo(file_item: cgi.FieldStorage) -> str:
    original_name = Path(file_item.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        raise ValueError("Photo must be a JPG, PNG, WEBP, or GIF file.")

    filename = f"{uuid.uuid4().hex}{extension}"
    target_path = UPLOADS_DIR / filename
    with open(target_path, "wb") as output_file:
        output_file.write(file_item.file.read())
    return filename


def render_app(options: dict) -> str:
    template = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Pond Diary</title>
  <style>
    :root {
      --bg: #f4f7f5;
      --surface: #ffffff;
      --surface-soft: #f7faf8;
      --surface-strong: #eef3f0;
      --text: #17211d;
      --muted: #66756f;
      --line: #dce5e0;
      --line-strong: #cbd7d0;
      --brand: #1a7f63;
      --brand-strong: #116149;
      --accent: #d2aa68;
      --danger: #b14d4d;
      --shadow: 0 18px 44px rgba(18, 31, 26, 0.08);
      --radius-lg: 24px;
      --radius-md: 18px;
      --radius-sm: 14px;
    }

    body.theme-black {
      --bg: #050505;
      --surface: #0f0f10;
      --surface-soft: #161719;
      --surface-strong: #1d1f22;
      --text: #f5f6f7;
      --muted: #a8afb5;
      --line: #2a2d31;
      --line-strong: #34393d;
      --brand: #f5f5f5;
      --brand-strong: #ffffff;
      --accent: #9b9b9b;
      --danger: #ff8c8c;
      --shadow: none;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background: var(--bg);
      color: var(--text);
    }

    .app {
      width: min(1220px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 24px 0 40px;
    }

    .hero {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      padding: 28px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: var(--surface-soft);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    h1 {
      margin: 14px 0 8px;
      font-size: clamp(2.1rem, 4vw, 4.2rem);
      line-height: 0.94;
      letter-spacing: -0.05em;
    }

    .hero p {
      margin: 0;
      max-width: 760px;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }

    .layout {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 20px;
      margin-top: 20px;
      align-items: start;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      padding: 20px;
    }

    .panel-title {
      margin: 0 0 14px;
      font-size: 1.08rem;
      letter-spacing: -0.02em;
    }

    .panel-copy {
      margin: 0 0 18px;
      color: var(--muted);
      line-height: 1.5;
    }

    .tabs {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 18px;
      padding: 6px;
      background: var(--surface-soft);
      border: 1px solid var(--line);
      border-radius: 16px;
    }

    .tab {
      border: 0;
      border-radius: 12px;
      background: transparent;
      color: var(--muted);
      padding: 12px 10px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }

    .tab.active {
      background: var(--text);
      color: var(--surface);
    }

    body.theme-black .tab.active {
      background: var(--surface-strong);
      color: var(--text);
      border: 1px solid var(--line-strong);
    }

    .form-panel { display: none; }
    .form-panel.active { display: block; }

    .field { margin-bottom: 14px; }
    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    label {
      display: block;
      margin-bottom: 7px;
      font-size: 0.9rem;
      font-weight: 700;
    }

    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-soft);
      color: var(--text);
      padding: 12px 14px;
      font: inherit;
      outline: none;
    }

    input:focus, textarea:focus {
      border-color: var(--brand);
      background: var(--surface);
    }

    textarea {
      min-height: 96px;
      resize: vertical;
    }

    .button {
      width: 100%;
      border: 0;
      border-radius: 14px;
      background: var(--brand);
      color: #ffffff;
      padding: 13px 16px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
    }

    body.theme-black .button {
      background: #f1f1f1;
      color: #0a0a0a;
    }

    .button:disabled {
      opacity: 0.65;
      cursor: wait;
    }

    .status {
      min-height: 24px;
      margin-top: 12px;
      font-weight: 700;
      color: var(--muted);
    }

    .status.success { color: var(--brand-strong); }
    .status.error { color: var(--danger); }

    .hint {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 0.92rem;
    }

    .feed {
      display: grid;
      gap: 14px;
    }

    .entry {
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      padding: 16px;
      background: var(--surface-soft);
    }

    .entry-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }

    .entry-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: auto;
    }

    .entry-type {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      background: var(--surface-strong);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 800;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .entry-delete {
      width: 28px;
      height: 28px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      color: var(--muted);
      font: inherit;
      font-weight: 900;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
      flex: 0 0 auto;
    }

    .entry-delete:hover {
      border-color: var(--danger);
      color: var(--danger);
    }

    body.theme-black .entry-delete {
      background: var(--surface-soft);
    }

    .entry h3 {
      margin: 8px 0 0;
      font-size: 1.08rem;
      letter-spacing: -0.02em;
    }

    .meta {
      color: var(--muted);
      font-size: 0.92rem;
    }

    .details {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }

    .pill {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 0.9rem;
      color: var(--muted);
    }

    .entry p {
      margin: 0;
      color: var(--text);
      line-height: 1.55;
    }

    .entry img {
      width: 100%;
      max-height: 340px;
      object-fit: cover;
      border-radius: 16px;
      border: 1px solid var(--line);
      margin-top: 12px;
    }

    .empty {
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius-md);
      padding: 22px;
      color: var(--muted);
      text-align: center;
      background: var(--surface-soft);
    }

    @media (max-width: 920px) {
      .layout { grid-template-columns: 1fr; }
    }

    @media (max-width: 640px) {
      .app { width: min(100vw - 16px, 100%); padding-top: 16px; }
      .hero, .panel { border-radius: 18px; }
      .tabs { grid-template-columns: 1fr; }
      .grid-2 { grid-template-columns: 1fr; }
      .entry-top { flex-direction: column; }
    }
  </style>
</head>
<body class=\"__BODY_CLASS__\">
  <main class=\"app\">
    <section class=\"hero\">
      <div class=\"eyebrow\">Pond Journal</div>
      <h1>Track pond care with a cleaner daily log.</h1>
      <p>Add water test results, treatments, and pond photos in one place. The interface stays simple on desktop and mobile, and the add-on settings can choose the starting mode and a black theme.</p>
    </section>

    <section class=\"layout\">
      <aside class=\"panel\">
        <h2 class=\"panel-title\">New entry</h2>
        <p class=\"panel-copy\">Choose the entry type, add the details, and save. Your configured default mode opens first every time.</p>

        <div class=\"tabs\">
          <button class=\"tab\" type=\"button\" data-target=\"water-form\" data-mode=\"water_test\">Water Test</button>
          <button class=\"tab\" type=\"button\" data-target=\"product-form\" data-mode=\"product\">Product</button>
          <button class=\"tab\" type=\"button\" data-target=\"photo-form\" data-mode=\"photo\">Photo</button>
        </div>

        <form id=\"water-form\" class=\"form-panel\">
          <div class=\"field\">
            <label for=\"water-date\">Test date</label>
            <input id=\"water-date\" name=\"eventDate\" type=\"date\" required>
          </div>
          <div class=\"grid-2\">
            <div class=\"field\">
              <label for=\"ph\">pH</label>
              <input id=\"ph\" name=\"ph\" type=\"text\" placeholder=\"7.4\">
            </div>
            <div class=\"field\">
              <label for=\"temperature\">Temperature</label>
              <input id=\"temperature\" name=\"temperature\" type=\"text\" placeholder=\"18 C\">
            </div>
          </div>
          <div class=\"grid-2\">
            <div class=\"field\">
              <label for=\"ammonia\">Ammonia</label>
              <input id=\"ammonia\" name=\"ammonia\" type=\"text\" placeholder=\"0 ppm\">
            </div>
            <div class=\"field\">
              <label for=\"nitrite\">Nitrite</label>
              <input id=\"nitrite\" name=\"nitrite\" type=\"text\" placeholder=\"0 ppm\">
            </div>
          </div>
          <div class=\"grid-2\">
            <div class=\"field\">
              <label for=\"nitrate\">Nitrate</label>
              <input id=\"nitrate\" name=\"nitrate\" type=\"text\" placeholder=\"10 ppm\">
            </div>
            <div class=\"field\">
              <label for=\"hardness\">KH / GH</label>
              <input id=\"hardness\" name=\"hardness\" type=\"text\" placeholder=\"KH 6 / GH 8\">
            </div>
          </div>
          <div class=\"field\">
            <label for=\"water-notes\">Notes</label>
            <textarea id=\"water-notes\" name=\"notes\" placeholder=\"Anything you noticed in the pond...\"></textarea>
          </div>
          <button class=\"button\" type=\"submit\">Save water test</button>
        </form>

        <form id=\"product-form\" class=\"form-panel\">
          <div class=\"field\">
            <label for=\"product-date\">Application date</label>
            <input id=\"product-date\" name=\"eventDate\" type=\"date\" required>
          </div>
          <div class=\"field\">
            <label for=\"product-name\">Product name</label>
            <input id=\"product-name\" name=\"productName\" type=\"text\" placeholder=\"Anti algae treatment\" required>
          </div>
          <div class=\"grid-2\">
            <div class=\"field\">
              <label for=\"dose\">Dose</label>
              <input id=\"dose\" name=\"dose\" type=\"text\" placeholder=\"50 ml\">
            </div>
            <div class=\"field\">
              <label for=\"purpose\">Purpose</label>
              <input id=\"purpose\" name=\"purpose\" type=\"text\" placeholder=\"Green water control\">
            </div>
          </div>
          <div class=\"field\">
            <label for=\"product-notes\">Notes</label>
            <textarea id=\"product-notes\" name=\"notes\" placeholder=\"Why you added it, fish reaction, follow-up...\"></textarea>
          </div>
          <button class=\"button\" type=\"submit\">Save product log</button>
        </form>

        <form id=\"photo-form\" class=\"form-panel\" enctype=\"multipart/form-data\">
          <div class=\"field\">
            <label for=\"photo-date\">Photo date</label>
            <input id=\"photo-date\" name=\"eventDate\" type=\"date\" required>
          </div>
          <div class=\"field\">
            <label for=\"photo-file\">Photo</label>
            <input id=\"photo-file\" name=\"photo\" type=\"file\" accept=\"image/*\" required>
          </div>
          <div class=\"field\">
            <label for=\"photo-description\">Description</label>
            <textarea id=\"photo-description\" name=\"description\" placeholder=\"What changed in the pond, plants, fish, water clarity...\"></textarea>
          </div>
          <button class=\"button\" type=\"submit\">Save photo entry</button>
        </form>

        <div id=\"status\" class=\"status\" aria-live=\"polite\"></div>
        <p class=\"hint\">The black mode and default entry type are configured from the add-on options in Home Assistant.</p>
      </aside>

      <section class=\"panel\">
        <h2 class=\"panel-title\">Entries</h2>
        <p class=\"panel-copy\">Everything appears in one reverse-chronological list so you can quickly compare readings, treatments, and visual changes.</p>
        <div id=\"feed\" class=\"feed\">
          <div class=\"empty\">No entries yet. Add the first pond update from the left panel.</div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const APP_DEFAULT_MODE = "__DEFAULT_MODE__";
    const tabs = Array.from(document.querySelectorAll(".tab"));
    const panels = Array.from(document.querySelectorAll(".form-panel"));
    const statusEl = document.getElementById("status");
    const feedEl = document.getElementById("feed");

    function setTodayDefaults() {
      const today = new Date().toISOString().slice(0, 10);
      document.querySelectorAll('input[type="date"]').forEach((input) => {
        if (!input.value) {
          input.value = today;
        }
      });
    }

    function setStatus(message, kind = "") {
      statusEl.textContent = message || "";
      statusEl.className = kind ? `status ${kind}` : "status";
    }

    function activateMode(mode) {
      const activeTab = tabs.find((tab) => tab.dataset.mode === mode) || tabs[0];
      tabs.forEach((tab) => tab.classList.toggle("active", tab === activeTab));
      panels.forEach((panel) => panel.classList.toggle("active", panel.id === activeTab.dataset.target));
      setStatus("");
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function renderDetails(entry) {
      const details = entry.details || {};
      const pills = [];
      if (entry.type === "water_test") {
        [["pH", details.ph], ["Temp", details.temperature], ["Ammonia", details.ammonia], ["Nitrite", details.nitrite], ["Nitrate", details.nitrate], ["Hardness", details.hardness]].forEach(([label, value]) => {
          if (value) {
            pills.push(`<span class="pill">${escapeHtml(label)}: ${escapeHtml(value)}</span>`);
          }
        });
      } else if (entry.type === "product") {
        [["Dose", details.dose], ["Purpose", details.purpose]].forEach(([label, value]) => {
          if (value) {
            pills.push(`<span class="pill">${escapeHtml(label)}: ${escapeHtml(value)}</span>`);
          }
        });
      }
      return pills.join("");
    }

    function typeLabel(type) {
      if (type === "water_test") return "Water Test";
      if (type === "product") return "Product";
      return "Photo";
    }

    function renderFeed(entries) {
      if (!entries.length) {
        feedEl.innerHTML = '<div class="empty">No entries yet. Add the first pond update from the left panel.</div>';
        return;
      }

      feedEl.innerHTML = entries.map((entry) => {
        const description = entry.description ? `<p>${escapeHtml(entry.description)}</p>` : "";
        const photo = entry.photoUrl ? `<img src="${encodeURI(entry.photoUrl)}" alt="Pond photo entry">` : "";
        return `
          <article class="entry">
            <div class="entry-top">
              <div>
                <div class="entry-type">${typeLabel(entry.type)}</div>
                <h3>${escapeHtml(entry.title)}</h3>
              </div>
              <div class="entry-actions">
                <div class="meta">${escapeHtml(entry.eventDate)}</div>
                <button class="entry-delete" type="button" data-entry-id="${entry.id}" aria-label="Remove entry" title="Remove entry">×</button>
              </div>
            </div>
            <div class="details">${renderDetails(entry)}</div>
            ${description}
            ${photo}
          </article>
        `;
      }).join("");
    }

    async function loadEntries() {
      const response = await fetch("/api/entries");
      const entries = await response.json();
      renderFeed(entries);
    }

    async function deleteEntry(entryId) {
      setStatus("Removing entry...");
      try {
        const response = await fetch("/api/entries/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: entryId }),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Unable to remove entry.");
        }
        setStatus("Entry removed.", "success");
        await loadEntries();
      } catch (error) {
        setStatus(error.message || "Unable to remove entry.", "error");
      }
    }

    async function submitJsonForm(form, url) {
      const submitButton = form.querySelector("button[type='submit']");
      submitButton.disabled = true;
      setStatus("Saving entry...");
      try {
        const payload = Object.fromEntries(new FormData(form).entries());
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Unable to save entry.");
        }
        form.reset();
        setTodayDefaults();
        setStatus("Entry saved.", "success");
        await loadEntries();
      } catch (error) {
        setStatus(error.message || "Unable to save entry.", "error");
      } finally {
        submitButton.disabled = false;
      }
    }

    async function submitPhotoForm(form) {
      const submitButton = form.querySelector("button[type='submit']");
      submitButton.disabled = true;
      setStatus("Uploading photo...");
      try {
        const response = await fetch("/api/photos", {
          method: "POST",
          body: new FormData(form),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Unable to save photo entry.");
        }
        form.reset();
        setTodayDefaults();
        setStatus("Photo entry saved.", "success");
        await loadEntries();
      } catch (error) {
        setStatus(error.message || "Unable to save photo entry.", "error");
      } finally {
        submitButton.disabled = false;
      }
    }

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => activateMode(tab.dataset.mode));
    });

    feedEl.addEventListener("click", (event) => {
      const button = event.target.closest(".entry-delete");
      if (!button) {
        return;
      }
      const entryId = Number(button.dataset.entryId);
      if (!entryId || !window.confirm("Remove this entry?")) {
        return;
      }
      deleteEntry(entryId);
    });

    document.getElementById("water-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitJsonForm(event.currentTarget, "/api/water-tests");
    });

    document.getElementById("product-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitJsonForm(event.currentTarget, "/api/products");
    });

    document.getElementById("photo-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitPhotoForm(event.currentTarget);
    });

    setTodayDefaults();
    activateMode(APP_DEFAULT_MODE);
    loadEntries().catch(() => {
      setStatus("Unable to load entries.", "error");
    });
  </script>
</body>
</html>
"""

    body_class = "theme-black" if options.get("black_mode") else ""
    return template.replace("__DEFAULT_MODE__", options["default_mode"]).replace("__BODY_CLASS__", body_class)


class PondDiaryHandler(BaseHTTPRequestHandler):
    server_version = "PondDiary/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, render_app(load_options()))
            return

        if parsed.path == "/api/entries":
            json_response(self, fetch_entries())
            return

        if parsed.path.startswith("/uploads/"):
            self.serve_upload(parsed.path)
            return

        json_response(self, {"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/water-tests":
                payload = parse_json(self)
                event_date = validate_event_date(payload.get("eventDate", ""))
                notes = normalize_text(payload.get("notes", ""))
                details = {
                    "ph": normalize_text(payload.get("ph", "")),
                    "temperature": normalize_text(payload.get("temperature", "")),
                    "ammonia": normalize_text(payload.get("ammonia", "")),
                    "nitrite": normalize_text(payload.get("nitrite", "")),
                    "nitrate": normalize_text(payload.get("nitrate", "")),
                    "hardness": normalize_text(payload.get("hardness", "")),
                }
                insert_entry("water_test", "Water test recorded", notes, event_date, details)
                json_response(self, {"status": "ok"}, HTTPStatus.CREATED)
                return

            if parsed.path == "/api/products":
                payload = parse_json(self)
                event_date = validate_event_date(payload.get("eventDate", ""))
                product_name = normalize_text(payload.get("productName", ""))
                if not product_name:
                    raise ValueError("Product name is required.")
                notes = normalize_text(payload.get("notes", ""))
                details = {
                    "dose": normalize_text(payload.get("dose", "")),
                    "purpose": normalize_text(payload.get("purpose", "")),
                }
                insert_entry("product", product_name, notes, event_date, details)
                json_response(self, {"status": "ok"}, HTTPStatus.CREATED)
                return

            if parsed.path == "/api/photos":
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                    },
                )
                event_date = validate_event_date(form.getfirst("eventDate", ""))
                description = normalize_text(form.getfirst("description", ""))
                photo_field = form["photo"] if "photo" in form else None
                if photo_field is None or not getattr(photo_field, "filename", ""):
                    raise ValueError("Photo file is required.")
                filename = save_uploaded_photo(photo_field)
                insert_entry(
                    "photo",
                    "Pond photo",
                    description,
                    event_date,
                    {"filename": filename},
                    photo_path=filename,
                )
                json_response(self, {"status": "ok"}, HTTPStatus.CREATED)
                return

            if parsed.path == "/api/entries/delete":
                payload = parse_json(self)
                try:
                    entry_id = int(payload.get("id", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("A valid entry id is required.") from exc
                if entry_id <= 0:
                    raise ValueError("A valid entry id is required.")
                if not delete_entry(entry_id):
                    json_response(self, {"error": "Entry not found."}, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, {"status": "ok"}, HTTPStatus.OK)
                return

            json_response(self, {"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            json_response(self, {"error": "Internal server error."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            print("Unhandled error while processing request:", file=sys.stderr)
            raise

    def serve_upload(self, path: str) -> None:
        filename = Path(unquote(path.removeprefix("/uploads/"))).name
        if not filename:
            json_response(self, {"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return

        file_path = UPLOADS_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            json_response(self, {"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return

        content_type, _ = mimetypes.guess_type(file_path.name)
        with open(file_path, "rb") as upload:
            payload = upload.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    ensure_storage()
    httpd = ThreadingHTTPServer((HOST, PORT), PondDiaryHandler)
    print(f"Pond Diary listening on {HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()





