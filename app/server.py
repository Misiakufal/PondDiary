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
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8099"))


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
        entry = {
            "id": row["id"],
            "type": row["entry_type"],
            "title": row["title"],
            "description": row["description"] or "",
            "eventDate": row["event_date"],
            "createdAt": row["created_at"],
            "details": details,
            "photoUrl": f"/uploads/{row['photo_path']}" if row["photo_path"] else None,
        }
        entries.append(entry)
    return entries


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


def render_app() -> str:
    return """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Pond Diary</title>
  <style>
    :root {
      --bg: #eef6f3;
      --panel: rgba(255, 255, 255, 0.9);
      --panel-strong: #ffffff;
      --text: #15352b;
      --muted: #5f746d;
      --line: rgba(21, 53, 43, 0.12);
      --brand: #1b7f63;
      --brand-deep: #115744;
      --warm: #d2a25a;
      --danger: #a94141;
      --shadow: 0 16px 40px rgba(20, 58, 48, 0.12);
      --radius: 22px;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: \"Segoe UI\", Tahoma, Geneva, Verdana, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(27, 127, 99, 0.16), transparent 30%),
        radial-gradient(circle at right, rgba(210, 162, 90, 0.18), transparent 25%),
        linear-gradient(180deg, #f7fbf8 0%, var(--bg) 100%);
      min-height: 100vh;
    }

    .shell {
      width: min(1180px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 20px 0 32px;
    }

    .hero {
      background: linear-gradient(135deg, rgba(255,255,255,0.82), rgba(255,255,255,0.96));
      border: 1px solid rgba(255,255,255,0.6);
      backdrop-filter: blur(8px);
      border-radius: 28px;
      box-shadow: var(--shadow);
      padding: 24px;
      overflow: hidden;
      position: relative;
    }

    .hero::after {
      content: \"\";
      position: absolute;
      inset: auto -40px -80px auto;
      width: 220px;
      height: 220px;
      background: radial-gradient(circle, rgba(27, 127, 99, 0.2), transparent 65%);
      pointer-events: none;
    }

    .hero h1 {
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.6rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }

    .hero p {
      margin: 10px 0 0;
      max-width: 760px;
      color: var(--muted);
      font-size: 1rem;
    }

    .layout {
      display: grid;
      grid-template-columns: 380px minmax(0, 1fr);
      gap: 20px;
      margin-top: 20px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.75);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 18px;
      backdrop-filter: blur(10px);
    }

    .tabs {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-bottom: 16px;
    }

    .tab {
      border: 0;
      border-radius: 999px;
      background: rgba(27, 127, 99, 0.08);
      color: var(--brand-deep);
      padding: 10px 12px;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.15s ease, background 0.15s ease;
    }

    .tab.active {
      background: var(--brand);
      color: #fff;
      transform: translateY(-1px);
    }

    .form-panel { display: none; }
    .form-panel.active { display: block; }

    label {
      display: block;
      font-size: 0.9rem;
      font-weight: 700;
      margin-bottom: 6px;
    }

    .field { margin-bottom: 12px; }
    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 13px;
      font: inherit;
      color: var(--text);
      background: rgba(255, 255, 255, 0.95);
    }

    textarea {
      min-height: 96px;
      resize: vertical;
    }

    .button {
      border: 0;
      border-radius: 999px;
      padding: 12px 16px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      background: linear-gradient(135deg, var(--brand), #25a07d);
      color: #fff;
      box-shadow: 0 10px 24px rgba(27, 127, 99, 0.24);
    }

    .button:disabled {
      opacity: 0.65;
      cursor: wait;
    }

    .hint, .empty, .meta, .details, .status {
      color: var(--muted);
    }

    .status {
      min-height: 24px;
      margin-top: 12px;
      font-weight: 700;
    }

    .status.error { color: var(--danger); }
    .status.success { color: var(--brand-deep); }

    .feed {
      display: grid;
      gap: 14px;
    }

    .entry {
      background: var(--panel-strong);
      border: 1px solid rgba(21, 53, 43, 0.08);
      border-radius: 20px;
      padding: 16px;
      display: grid;
      gap: 10px;
    }

    .entry-header {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 10px;
    }

    .entry-type {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      background: rgba(27, 127, 99, 0.08);
      color: var(--brand-deep);
      font-size: 0.85rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .entry h3 {
      margin: 0;
      font-size: 1.1rem;
    }

    .details {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.92rem;
    }

    .pill {
      background: rgba(21, 53, 43, 0.05);
      border-radius: 999px;
      padding: 6px 10px;
    }

    .entry img {
      width: 100%;
      max-height: 320px;
      object-fit: cover;
      border-radius: 16px;
      border: 1px solid rgba(21, 53, 43, 0.08);
    }

    @media (max-width: 920px) {
      .layout { grid-template-columns: 1fr; }
    }

    @media (max-width: 600px) {
      .shell { width: min(100vw - 16px, 100%); padding-top: 12px; }
      .hero, .panel { border-radius: 22px; }
      .grid-2 { grid-template-columns: 1fr; }
      .tabs { grid-template-columns: 1fr; }
      .entry-header { flex-direction: column; }
    }
  </style>
</head>
<body>
  <main class=\"shell\">
    <section class=\"hero\">
      <h1>Pond Diary</h1>
      <p>Keep one clear timeline of water test readings, treatments, and pond photos. The layout works well on a desktop browser and scales down cleanly on mobile.</p>
    </section>

    <section class=\"layout\">
      <aside class=\"panel\">
        <div class=\"tabs\">
          <button class=\"tab active\" type=\"button\" data-target=\"water-form\">Water Test</button>
          <button class=\"tab\" type=\"button\" data-target=\"product-form\">Product</button>
          <button class=\"tab\" type=\"button\" data-target=\"photo-form\">Photo</button>
        </div>

        <form id=\"water-form\" class=\"form-panel active\">
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
              <label for=\"kh\">KH / GH</label>
              <input id=\"kh\" name=\"hardness\" type=\"text\" placeholder=\"KH 6 / GH 8\">
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
        <p class=\"hint\">Photos are stored inside the add-on data folder, so entries remain available after restarts.</p>
      </aside>

      <section class=\"panel\">
        <div class=\"entry-header\">
          <div>
            <h2 style=\"margin:0;\">Recent entries</h2>
            <p class=\"meta\" style=\"margin:6px 0 0;\">Your main screen shows water tests, products, and photos in one timeline.</p>
          </div>
        </div>
        <div id=\"feed\" class=\"feed\">
          <div class=\"empty\">No entries yet. Add your first pond update from the panel on the left.</div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const tabs = Array.from(document.querySelectorAll(\".tab\"));
    const panels = Array.from(document.querySelectorAll(\".form-panel\"));
    const statusEl = document.getElementById(\"status\");
    const feedEl = document.getElementById(\"feed\");

    function setTodayDefaults() {
      const today = new Date().toISOString().slice(0, 10);
      for (const input of document.querySelectorAll('input[type=\"date\"]')) {
        input.value = today;
      }
    }

    function setStatus(message, kind = \"\") {
      statusEl.textContent = message || \"\";
      statusEl.className = kind ? `status ${kind}` : \"status\";
    }

    function escapeHtml(value) {
      return value
        .replaceAll(\"&\", \"&amp;\")
        .replaceAll(\"<\", \"&lt;\")
        .replaceAll(\">\", \"&gt;\")
        .replaceAll("\"", "&quot;")
        .replaceAll(\"'\", \"&#39;\");
    }

    function renderDetails(entry) {
      const pairs = [];
      const details = entry.details || {};
      if (entry.type === \"water_test\") {
        [[\"pH\", details.ph], [\"Temp\", details.temperature], [\"Ammonia\", details.ammonia], [\"Nitrite\", details.nitrite], [\"Nitrate\", details.nitrate], [\"Hardness\", details.hardness]].forEach(([label, value]) => {
          if (value) pairs.push(`<span class=\"pill\">${escapeHtml(label)}: ${escapeHtml(value)}</span>`);
        });
      } else if (entry.type === \"product\") {
        [[\"Dose\", details.dose], [\"Purpose\", details.purpose]].forEach(([label, value]) => {
          if (value) pairs.push(`<span class=\"pill\">${escapeHtml(label)}: ${escapeHtml(value)}</span>`);
        });
      }
      return pairs.join(\"\");
    }

    function typeLabel(type) {
      if (type === \"water_test\") return \"Water Test\";
      if (type === \"product\") return \"Product\";
      return \"Photo\";
    }

    function renderFeed(entries) {
      if (!entries.length) {
        feedEl.innerHTML = '<div class=\"empty\">No entries yet. Add your first pond update from the panel on the left.</div>';
        return;
      }

      feedEl.innerHTML = entries.map((entry) => {
        const description = entry.description ? `<p style=\"margin:0;\">${escapeHtml(entry.description)}</p>` : \"\";
        const photo = entry.photoUrl ? `<img src=\"${encodeURI(entry.photoUrl)}\" alt=\"Pond photo entry\">` : \"\";
        return `
          <article class=\"entry\">
            <div class=\"entry-header\">
              <div>
                <div class=\"entry-type\">${typeLabel(entry.type)}</div>
                <h3>${escapeHtml(entry.title)}</h3>
              </div>
              <div class=\"meta\">${escapeHtml(entry.eventDate)}</div>
            </div>
            <div class=\"details\">${renderDetails(entry)}</div>
            ${description}
            ${photo}
          </article>
        `;
      }).join(\"\");
    }

    async function loadEntries() {
      const response = await fetch(\"/api/entries\");
      const entries = await response.json();
      renderFeed(entries);
    }

    async function submitJsonForm(form, url) {
      const submitButton = form.querySelector(\"button[type='submit']\");
      submitButton.disabled = true;
      setStatus(\"Saving entry...\");
      try {
        const formData = new FormData(form);
        const payload = Object.fromEntries(formData.entries());
        const response = await fetch(url, {
          method: \"POST\",
          headers: { \"Content-Type\": \"application/json\" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || \"Unable to save entry.\");
        }
        form.reset();
        setTodayDefaults();
        setStatus(\"Entry saved.\", \"success\");
        await loadEntries();
      } catch (error) {
        setStatus(error.message, \"error\");
      } finally {
        submitButton.disabled = false;
      }
    }

    async function submitPhotoForm(form) {
      const submitButton = form.querySelector(\"button[type='submit']\");
      submitButton.disabled = true;
      setStatus(\"Uploading photo...\");
      try {
        const response = await fetch(\"/api/photos\", {
          method: \"POST\",
          body: new FormData(form)
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || \"Unable to save photo entry.\");
        }
        form.reset();
        setTodayDefaults();
        setStatus(\"Photo entry saved.\", \"success\");
        await loadEntries();
      } catch (error) {
        setStatus(error.message, \"error\");
      } finally {
        submitButton.disabled = false;
      }
    }

    tabs.forEach((tab) => {
      tab.addEventListener(\"click\", () => {
        tabs.forEach((item) => item.classList.toggle(\"active\", item === tab));
        panels.forEach((panel) => panel.classList.toggle(\"active\", panel.id === tab.dataset.target));
        setStatus(\"\");
      });
    });

    document.getElementById(\"water-form\").addEventListener(\"submit\", (event) => {
      event.preventDefault();
      submitJsonForm(event.currentTarget, \"/api/water-tests\");
    });

    document.getElementById(\"product-form\").addEventListener(\"submit\", (event) => {
      event.preventDefault();
      submitJsonForm(event.currentTarget, \"/api/products\");
    });

    document.getElementById(\"photo-form\").addEventListener(\"submit\", (event) => {
      event.preventDefault();
      submitPhotoForm(event.currentTarget);
    });

    setTodayDefaults();
    loadEntries().catch(() => {
      setStatus(\"Unable to load entries.\", \"error\");
    });
  </script>
</body>
</html>
"""


class PondDiaryHandler(BaseHTTPRequestHandler):
    server_version = "PondDiary/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, render_app())
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
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": self.headers.get("Content-Length", "0")})
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

