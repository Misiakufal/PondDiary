
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO products (name, created_at)
            SELECT DISTINCT title, created_at
            FROM entries
            WHERE entry_type = 'product' AND title != ''
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


def fetch_products() -> list[dict]:
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT id, name FROM products ORDER BY LOWER(name), id"
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def get_product_name(product_id: int) -> str | None:
    with db_connection() as connection:
        row = connection.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
    return row["name"] if row else None


def add_product(name: str) -> dict:
    cleaned = normalize_text(name)
    if not cleaned:
        raise ValueError("Product name is required.")
    with db_connection() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO products (name, created_at) VALUES (?, ?)",
                (cleaned, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("That product already exists.") from exc
        connection.commit()
        return {"id": cursor.lastrowid, "name": cleaned}


def rename_product(product_id: int, new_name: str) -> dict:
    cleaned = normalize_text(new_name)
    if not cleaned:
        raise ValueError("Product name is required.")
    with db_connection() as connection:
        row = connection.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise ValueError("Product not found.")
        old_name = row["name"]
        try:
            connection.execute("UPDATE products SET name = ? WHERE id = ?", (cleaned, product_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("That product already exists.") from exc
        connection.execute(
            "UPDATE entries SET title = ? WHERE entry_type = 'product' AND title = ?",
            (cleaned, old_name),
        )
        connection.commit()
    return {"id": product_id, "name": cleaned}


def remove_product(product_id: int) -> bool:
    with db_connection() as connection:
        cursor = connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        connection.commit()
    return cursor.rowcount > 0


def delete_entry(entry_id: int) -> bool:
    with db_connection() as connection:
        row = connection.execute("SELECT photo_path FROM entries WHERE id = ?", (entry_id,)).fetchone()
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
      --danger: #b14d4d;
      --shadow: 0 18px 44px rgba(18, 31, 26, 0.08);
      --radius-lg: 24px;
      --radius-md: 18px;
      --radius-sm: 14px;
      --metric-ph: #1a7f63;
      --metric-temperature: #d18a27;
      --metric-ammonia: #c14e4e;
      --metric-nitrite: #8651dd;
      --metric-nitrate: #2374b9;
      --metric-hardness: #5d6f7e;
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
      --danger: #ff8c8c;
      --shadow: none;
      --metric-ph: #6be1bd;
      --metric-temperature: #ffc05f;
      --metric-ammonia: #ff8c8c;
      --metric-nitrite: #be9cff;
      --metric-nitrate: #74bcff;
      --metric-hardness: #c0ced8;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; background: var(--bg); color: var(--text); }
    .app { width: min(1260px, calc(100vw - 24px)); margin: 0 auto; padding: 24px 0 40px; }
    .hero, .panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-lg); box-shadow: var(--shadow); }
    .hero { padding: 28px; }
    .eyebrow { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: var(--surface-soft); border: 1px solid var(--line); color: var(--muted); font-size: .82rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
    h1 { margin: 14px 0 8px; font-size: clamp(2.1rem, 4vw, 4.2rem); line-height: .94; letter-spacing: -.05em; }
    .hero p { margin: 0; max-width: 760px; color: var(--muted); font-size: 1rem; line-height: 1.6; }
    .layout { display: grid; grid-template-columns: 360px minmax(0, 1fr); gap: 20px; margin-top: 20px; align-items: start; }
    .panel { padding: 20px; }
    .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    .panel-title { margin: 0 0 10px; font-size: 1.08rem; letter-spacing: -.02em; }
    .panel-copy { margin: 0; color: var(--muted); line-height: 1.5; }
    .tabs, .view-toggle, .range-toggle { display: grid; gap: 8px; padding: 6px; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 16px; }
    .tabs { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-bottom: 18px; }
    .view-toggle { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .range-toggle { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 18px; }
    .tab, .view-button, .range-button, .small-button { border: 0; border-radius: 12px; background: transparent; color: var(--muted); padding: 12px 10px; font: inherit; font-weight: 700; cursor: pointer; }
    .tab.active, .view-button.active, .range-button.active { background: var(--text); color: var(--surface); }
    body.theme-black .tab.active, body.theme-black .view-button.active, body.theme-black .range-button.active { background: var(--surface-strong); color: var(--text); border: 1px solid var(--line-strong); }
    .form-panel, .main-view { display: none; }
    .form-panel.active, .main-view.active { display: block; }
    .field { margin-bottom: 14px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    label { display: block; margin-bottom: 7px; font-size: .9rem; font-weight: 700; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: var(--radius-sm); background: var(--surface-soft); color: var(--text); padding: 12px 14px; font: inherit; outline: none; }
    input:focus, textarea:focus, select:focus { border-color: var(--brand); background: var(--surface); }
    textarea { min-height: 96px; resize: vertical; }
    .button { width: 100%; border: 0; border-radius: 14px; background: var(--brand); color: #fff; padding: 13px 16px; font: inherit; font-weight: 800; cursor: pointer; }
    body.theme-black .button { background: #f1f1f1; color: #0a0a0a; }
    .button:disabled, .small-button:disabled { opacity: .65; cursor: wait; }
    .status { min-height: 24px; margin-top: 12px; font-weight: 700; color: var(--muted); }
    .status.success { color: var(--brand-strong); }
    .status.error { color: var(--danger); }
    .hint { margin: 12px 0 0; color: var(--muted); line-height: 1.5; font-size: .92rem; }
    .feed { display: grid; gap: 14px; }
    .entry { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 16px; background: var(--surface-soft); }
    .entry-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .entry-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
    .entry-type { display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--surface-strong); border: 1px solid var(--line); color: var(--muted); font-size: .78rem; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
    .entry-delete { width: 28px; height: 28px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); color: var(--muted); font: inherit; font-weight: 900; line-height: 1; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; padding: 0; flex: 0 0 auto; }
    .entry-delete:hover, .product-remove:hover { border-color: var(--danger); color: var(--danger); }
    .entry h3 { margin: 8px 0 0; font-size: 1.08rem; letter-spacing: -.02em; }
    .meta, .chart-subtle { color: var(--muted); font-size: .92rem; }
    .details, .metric-choices { display: flex; flex-wrap: wrap; gap: 8px; }
    .details { margin-bottom: 10px; }
    .pill, .metric-choice { background: var(--surface); border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; font-size: .9rem; color: var(--muted); }
    .metric-choice { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
    .metric-choice input { width: 14px; height: 14px; margin: 0; accent-color: var(--brand); }
    .metric-swatch { width: 10px; height: 10px; border-radius: 999px; display: inline-block; }
    .entry p { margin: 0; color: var(--text); line-height: 1.55; }
    .entry img { width: 100%; max-height: 340px; object-fit: cover; border-radius: 16px; border: 1px solid var(--line); margin-top: 12px; }
    .empty, .chart-empty, .products-empty { border: 1px dashed var(--line-strong); border-radius: var(--radius-md); padding: 22px; color: var(--muted); text-align: center; background: var(--surface-soft); }
    .chart-shell { border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--surface-soft); padding: 14px; }
    .chart-toolbar { display: grid; gap: 14px; }
    .chart-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; }
    .chart-frame { position: relative; height: 420px; margin-top: 14px; border: 1px solid var(--line); border-radius: 16px; overflow: hidden; background: var(--surface); touch-action: none; cursor: grab; }
    .chart-frame.dragging { cursor: grabbing; }
    .chart-canvas { width: 100%; height: 100%; display: block; }
    .chart-tooltip { position: absolute; pointer-events: none; min-width: 160px; max-width: 240px; padding: 10px 12px; border-radius: 12px; background: rgba(12,16,14,.92); color: #fff; font-size: .86rem; line-height: 1.45; transform: translate(-50%, calc(-100% - 14px)); opacity: 0; transition: opacity .12s ease; z-index: 2; }
    .chart-tooltip.visible { opacity: 1; }
    body.theme-black .chart-tooltip { background: rgba(255,255,255,.96); color: #111; }
    .chart-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }
    .ghost-button, .small-button, .product-remove { border: 1px solid var(--line); background: var(--surface); color: var(--text); border-radius: 12px; padding: 10px 12px; font: inherit; font-weight: 700; cursor: pointer; }
    .range-note { margin-top: 8px; color: var(--muted); font-size: .9rem; }
    .products-stack { display: grid; gap: 16px; }
    .products-add { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 10px; }
    .products-list { display: grid; gap: 10px; }
    .product-row { display: grid; grid-template-columns: minmax(0,1fr) auto auto; gap: 10px; align-items: center; border: 1px solid var(--line); border-radius: 14px; padding: 12px; background: var(--surface-soft); }
    .product-remove { color: var(--danger); }
    @media (max-width: 920px) { .layout { grid-template-columns: 1fr; } }
    @media (max-width: 640px) { .app { width: min(100vw - 16px, 100%); padding-top: 16px; } .hero, .panel { border-radius: 18px; } .tabs, .range-toggle, .view-toggle, .products-add, .grid-2, .product-row { grid-template-columns: 1fr; } .entry-top, .chart-meta, .panel-head { flex-direction: column; } .chart-frame { height: 340px; } .chart-actions { justify-content: stretch; } .ghost-button, .small-button, .product-remove { width: 100%; } }
  </style>
</head>
<body class=\"__BODY_CLASS__\">
  <main class=\"app\">
    <section class=\"hero\"><div class=\"eyebrow\">Pond Journal</div><h1>Track pond care with a cleaner daily log.</h1><p>Add water test results, treatments, pond photos, and manage a reusable product list for your pond diary.</p></section>
    <section class=\"layout\">
      <aside class=\"panel\">
        <h2 class=\"panel-title\">New entry</h2>
        <p class=\"panel-copy\">Choose the entry type, add the details, and save. Product entries now use the managed product catalog.</p>
        <div class=\"tabs\">
          <button class=\"tab\" type=\"button\" data-target=\"water-form\" data-mode=\"water_test\">Water Test</button>
          <button class=\"tab\" type=\"button\" data-target=\"product-form\" data-mode=\"product\">Product</button>
          <button class=\"tab\" type=\"button\" data-target=\"photo-form\" data-mode=\"photo\">Photo</button>
        </div>
        <form id=\"water-form\" class=\"form-panel\"><div class=\"field\"><label for=\"water-date\">Test date</label><input id=\"water-date\" name=\"eventDate\" type=\"date\" required></div><div class=\"grid-2\"><div class=\"field\"><label for=\"ph\">pH</label><input id=\"ph\" name=\"ph\" type=\"text\" placeholder=\"7.4\"></div><div class=\"field\"><label for=\"temperature\">Temperature</label><input id=\"temperature\" name=\"temperature\" type=\"text\" placeholder=\"18 C\"></div></div><div class=\"grid-2\"><div class=\"field\"><label for=\"ammonia\">Ammonia</label><input id=\"ammonia\" name=\"ammonia\" type=\"text\" placeholder=\"0 ppm\"></div><div class=\"field\"><label for=\"nitrite\">Nitrite</label><input id=\"nitrite\" name=\"nitrite\" type=\"text\" placeholder=\"0 ppm\"></div></div><div class=\"grid-2\"><div class=\"field\"><label for=\"nitrate\">Nitrate</label><input id=\"nitrate\" name=\"nitrate\" type=\"text\" placeholder=\"10 ppm\"></div><div class=\"field\"><label for=\"hardness\">KH / GH</label><input id=\"hardness\" name=\"hardness\" type=\"text\" placeholder=\"KH 6 / GH 8\"></div></div><div class=\"field\"><label for=\"water-notes\">Notes</label><textarea id=\"water-notes\" name=\"notes\" placeholder=\"Anything you noticed in the pond...\"></textarea></div><button class=\"button\" type=\"submit\">Save water test</button></form>
        <form id=\"product-form\" class=\"form-panel\"><div class=\"field\"><label for=\"product-date\">Application date</label><input id=\"product-date\" name=\"eventDate\" type=\"date\" required></div><div class=\"field\"><label for=\"product-select\">Product</label><select id=\"product-select\" name=\"productId\" required></select></div><div class=\"grid-2\"><div class=\"field\"><label for=\"dose\">Dose</label><input id=\"dose\" name=\"dose\" type=\"text\" placeholder=\"50 ml\"></div><div class=\"field\"><label for=\"purpose\">Purpose</label><input id=\"purpose\" name=\"purpose\" type=\"text\" placeholder=\"Green water control\"></div></div><div class=\"field\"><label for=\"product-notes\">Notes</label><textarea id=\"product-notes\" name=\"notes\" placeholder=\"Why you added it, fish reaction, follow-up...\"></textarea></div><button id=\"product-submit\" class=\"button\" type=\"submit\">Save product log</button></form>
        <form id=\"photo-form\" class=\"form-panel\" enctype=\"multipart/form-data\"><div class=\"field\"><label for=\"photo-date\">Photo date</label><input id=\"photo-date\" name=\"eventDate\" type=\"date\" required></div><div class=\"field\"><label for=\"photo-file\">Photo</label><input id=\"photo-file\" name=\"photo\" type=\"file\" accept=\"image/*\" required></div><div class=\"field\"><label for=\"photo-description\">Description</label><textarea id=\"photo-description\" name=\"description\" placeholder=\"What changed in the pond, plants, fish, water clarity...\"></textarea></div><button class=\"button\" type=\"submit\">Save photo entry</button></form>
        <div id=\"status\" class=\"status\" aria-live=\"polite\"></div>
        <p class=\"hint\">Manage products in the Products view, then select them here when you log treatments.</p>
      </aside>
      <section class=\"panel\">
        <div class=\"panel-head\">
          <div><h2 class=\"panel-title\">Pond activity</h2><p class=\"panel-copy\">Switch between the activity log, water test chart, and product catalog management.</p></div>
          <div class=\"view-toggle\">
            <button class=\"view-button active\" type=\"button\" data-view=\"log\">Log</button>
            <button class=\"view-button\" type=\"button\" data-view=\"chart\">Chart</button>
            <button class=\"view-button\" type=\"button\" data-view=\"products\">Products</button>
          </div>
        </div>
        <div id=\"log-view\" class=\"main-view active\"><div id=\"feed\" class=\"feed\"><div class=\"empty\">No entries yet. Add the first pond update from the left panel.</div></div></div>
        <div id=\"chart-view\" class=\"main-view\"><div class=\"chart-toolbar\"><div><div class=\"metric-choices\" id=\"metric-choices\"></div><div class=\"range-note\">Use mouse wheel to zoom and drag the chart to pan through time.</div></div><div class=\"chart-shell\"><div class=\"chart-meta\"><div class=\"chart-subtle\" id=\"chart-summary\">Select metrics to compare water readings over time.</div><div class=\"chart-actions\"><button class=\"ghost-button\" id=\"reset-zoom\" type=\"button\">Reset zoom</button></div></div><div id=\"chart-frame\" class=\"chart-frame\"><canvas id=\"chart-canvas\" class=\"chart-canvas\"></canvas><div id=\"chart-tooltip\" class=\"chart-tooltip\"></div></div><div class=\"range-toggle\"><button class=\"range-button active\" type=\"button\" data-range=\"week\">Last week</button><button class=\"range-button\" type=\"button\" data-range=\"month\">Last month</button><button class=\"range-button\" type=\"button\" data-range=\"year\">Last year</button></div></div></div><div id=\"chart-empty\" class=\"chart-empty\" style=\"display:none;\">Add water test entries with numeric values to populate the chart.</div></div>
        <div id=\"products-view\" class=\"main-view\"><div class=\"products-stack\"><div><h3 class=\"panel-title\" style=\"margin-bottom:8px;\">Product list</h3><p class=\"panel-copy\">Add products once, then reuse them for treatment entries.</p></div><form id=\"product-catalog-form\" class=\"products-add\"><input id=\"new-product-name\" name=\"name\" type=\"text\" placeholder=\"Add a new pond product\" required><button class=\"small-button\" type=\"submit\">Add product</button></form><div id=\"products-empty\" class=\"products-empty\" style=\"display:none;\">No products saved yet. Add the first one above.</div><div id=\"products-list\" class=\"products-list\"></div></div></div>
      </section>
    </section>
  </main>
  <script>
    const APP_DEFAULT_MODE = "__DEFAULT_MODE__";
    const METRICS = [
      { key: "ph", label: "pH", css: "--metric-ph" },
      { key: "temperature", label: "Temperature", css: "--metric-temperature" },
      { key: "ammonia", label: "Ammonia", css: "--metric-ammonia" },
      { key: "nitrite", label: "Nitrite", css: "--metric-nitrite" },
      { key: "nitrate", label: "Nitrate", css: "--metric-nitrate" },
      { key: "hardness", label: "Hardness", css: "--metric-hardness" },
    ];
    const RANGE_DAYS = { week: 7, month: 31, year: 366 };
    const tabs = Array.from(document.querySelectorAll(".tab"));
    const panels = Array.from(document.querySelectorAll(".form-panel"));
    const statusEl = document.getElementById("status");
    const feedEl = document.getElementById("feed");
    const viewButtons = Array.from(document.querySelectorAll(".view-button"));
    const mainViews = Array.from(document.querySelectorAll(".main-view"));
    const rangeButtons = Array.from(document.querySelectorAll(".range-button"));
    const metricChoicesEl = document.getElementById("metric-choices");
    const chartSummaryEl = document.getElementById("chart-summary");
    const chartEmptyEl = document.getElementById("chart-empty");
    const chartFrameEl = document.getElementById("chart-frame");
    const chartCanvasEl = document.getElementById("chart-canvas");
    const chartTooltipEl = document.getElementById("chart-tooltip");
    const resetZoomButton = document.getElementById("reset-zoom");
    const productSelectEl = document.getElementById("product-select");
    const productSubmitEl = document.getElementById("product-submit");
    const productsListEl = document.getElementById("products-list");
    const productsEmptyEl = document.getElementById("products-empty");
    const chartContext = chartCanvasEl.getContext("2d");
    const appState = {
      entries: [],
      products: [],
      chartRange: "week",
      activeMetrics: new Set(["ph", "temperature"]),
      currentView: "log",
      chart: { preparedPoints: [], domainStart: 0, domainEnd: 0, minTime: 0, maxTime: 0, dragging: false, dragStartX: 0, dragDomainStart: 0, dragDomainEnd: 0, hoverPoint: null, pixelPoints: [] },
    };
    function setTodayDefaults() { const today = new Date().toISOString().slice(0, 10); document.querySelectorAll('input[type="date"]').forEach((input) => { if (!input.value) input.value = today; }); }
    function setStatus(message, kind = "") { statusEl.textContent = message || ""; statusEl.className = kind ? `status ${kind}` : "status"; }
    function activateMode(mode) { const activeTab = tabs.find((tab) => tab.dataset.mode === mode) || tabs[0]; tabs.forEach((tab) => tab.classList.toggle("active", tab === activeTab)); panels.forEach((panel) => panel.classList.toggle("active", panel.id === activeTab.dataset.target)); setStatus(""); }
    function activateMainView(view) { appState.currentView = view; viewButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === view)); mainViews.forEach((panel) => panel.classList.toggle("active", panel.id === `${view}-view`)); if (view === "chart") renderChart(); }
    function escapeHtml(value) { return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
    function renderDetails(entry) { const details = entry.details || {}; const pills = []; if (entry.type === "water_test") { [["pH", details.ph], ["Temp", details.temperature], ["Ammonia", details.ammonia], ["Nitrite", details.nitrite], ["Nitrate", details.nitrate], ["Hardness", details.hardness]].forEach(([label, value]) => { if (value) pills.push(`<span class="pill">${escapeHtml(label)}: ${escapeHtml(value)}</span>`); }); } else if (entry.type === "product") { [["Dose", details.dose], ["Purpose", details.purpose]].forEach(([label, value]) => { if (value) pills.push(`<span class="pill">${escapeHtml(label)}: ${escapeHtml(value)}</span>`); }); } return pills.join(""); }
    function typeLabel(type) { if (type === "water_test") return "Water Test"; if (type === "product") return "Product"; return "Photo"; }
    function parseNumericValue(value) { const match = String(value ?? "").replace(",", ".").match(/-?\d+(?:\.\d+)?/); return match ? Number(match[0]) : null; }
    function getMetricColor(metricKey) { const metric = METRICS.find((item) => item.key === metricKey); return getComputedStyle(document.body).getPropertyValue(metric.css).trim() || "#1a7f63"; }
    function formatDate(date) { return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
    function buildChartSeries(entries) { return entries.filter((entry) => entry.type === "water_test").map((entry) => { const date = new Date(`${entry.eventDate}T12:00:00`); return { id: entry.id, date, timestamp: date.getTime(), values: Object.fromEntries(METRICS.map((metric) => [metric.key, parseNumericValue(entry.details?.[metric.key])])) }; }).filter((entry) => !Number.isNaN(entry.timestamp)).sort((a, b) => a.timestamp - b.timestamp); }
    function renderMetricChoices() { metricChoicesEl.innerHTML = METRICS.map((metric) => `<label class="metric-choice"><input type="checkbox" data-metric="${metric.key}" ${appState.activeMetrics.has(metric.key) ? "checked" : ""}><span class="metric-swatch" style="background:${getMetricColor(metric.key)}"></span><span>${metric.label}</span></label>`).join(""); }
    function renderFeed(entries) { if (!entries.length) { feedEl.innerHTML = '<div class="empty">No entries yet. Add the first pond update from the left panel.</div>'; return; } feedEl.innerHTML = entries.map((entry) => { const description = entry.description ? `<p>${escapeHtml(entry.description)}</p>` : ""; const photo = entry.photoUrl ? `<img src="${encodeURI(entry.photoUrl)}" alt="Pond photo entry">` : ""; return `<article class="entry"><div class="entry-top"><div><div class="entry-type">${typeLabel(entry.type)}</div><h3>${escapeHtml(entry.title)}</h3></div><div class="entry-actions"><div class="meta">${escapeHtml(entry.eventDate)}</div><button class="entry-delete" type="button" data-entry-id="${entry.id}" aria-label="Remove entry" title="Remove entry">x</button></div></div><div class="details">${renderDetails(entry)}</div>${description}${photo}</article>`; }).join(""); }
    function renderProductsCatalog() { if (!appState.products.length) { productsListEl.innerHTML = ""; productsEmptyEl.style.display = "block"; } else { productsEmptyEl.style.display = "none"; productsListEl.innerHTML = appState.products.map((product) => `<div class="product-row" data-product-id="${product.id}"><input class="product-name-input" type="text" value="${escapeHtml(product.name)}" aria-label="Product name"><button class="small-button product-rename" type="button">Save</button><button class="product-remove" type="button">Remove</button></div>`).join(""); } }
    function populateProductSelect() { if (!appState.products.length) { productSelectEl.innerHTML = '<option value="">No products yet. Add one in the Products view.</option>'; productSelectEl.disabled = true; productSubmitEl.disabled = true; return; } productSelectEl.innerHTML = '<option value="">Select a product</option>' + appState.products.map((product) => `<option value="${product.id}">${escapeHtml(product.name)}</option>`).join(""); productSelectEl.disabled = false; productSubmitEl.disabled = false; }
    function resizeCanvas() { const bounds = chartFrameEl.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1; chartCanvasEl.width = Math.max(1, Math.floor(bounds.width * dpr)); chartCanvasEl.height = Math.max(1, Math.floor(bounds.height * dpr)); chartCanvasEl.style.width = `${bounds.width}px`; chartCanvasEl.style.height = `${bounds.height}px`; chartContext.setTransform(dpr, 0, 0, dpr, 0, 0); }
    function getPreparedPoints() { const series = buildChartSeries(appState.entries); const now = series.length ? series[series.length - 1].timestamp : Date.now(); const rangeStart = now - RANGE_DAYS[appState.chartRange] * 86400000; const filtered = series.filter((point) => point.timestamp >= rangeStart); return filtered.length ? filtered : series; }
    function clampDomain(start, end, min, max) { const span = end - start; if (span <= 0) return { start: min, end: max }; if (start < min) return { start: min, end: min + span }; if (end > max) return { start: max - span, end: max }; return { start, end }; }
    function resetChartDomain(preparedPoints) { if (!preparedPoints.length) { appState.chart.preparedPoints = []; appState.chart.hoverPoint = null; return; } const minTime = preparedPoints[0].timestamp; const maxTime = preparedPoints[preparedPoints.length - 1].timestamp; const safeMax = maxTime === minTime ? maxTime + 86400000 : maxTime; Object.assign(appState.chart, { preparedPoints, minTime, maxTime: safeMax, domainStart: minTime, domainEnd: safeMax, hoverPoint: null }); }
    function ensureChartDomain(preparedPoints) { if (!preparedPoints.length) { appState.chart.preparedPoints = []; return false; } const minTime = preparedPoints[0].timestamp; const maxTime = preparedPoints[preparedPoints.length - 1].timestamp; const safeMax = maxTime === minTime ? maxTime + 86400000 : maxTime; const changed = !appState.chart.preparedPoints.length || appState.chart.minTime !== minTime || appState.chart.maxTime !== safeMax || appState.chart.preparedPoints.length !== preparedPoints.length; if (changed) { resetChartDomain(preparedPoints); } else { const clamped = clampDomain(appState.chart.domainStart, appState.chart.domainEnd, minTime, safeMax); Object.assign(appState.chart, { preparedPoints, minTime, maxTime: safeMax, domainStart: clamped.start, domainEnd: clamped.end }); } return true; }
    function drawChartGrid(ctx, width, height, chartArea, minY, maxY) { ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--line").trim(); ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--muted").trim(); ctx.lineWidth = 1; ctx.font = '12px "Segoe UI", sans-serif'; for (let index = 0; index <= 4; index += 1) { const y = chartArea.top + ((chartArea.bottom - chartArea.top) / 4) * index; ctx.beginPath(); ctx.moveTo(chartArea.left, y); ctx.lineTo(chartArea.right, y); ctx.stroke(); const value = maxY - ((maxY - minY) / 4) * index; ctx.fillText(value.toFixed(2).replace(/\.00$/, ""), 8, y + 4); } const domainSpan = appState.chart.domainEnd - appState.chart.domainStart; for (let index = 0; index <= 5; index += 1) { const x = chartArea.left + ((chartArea.right - chartArea.left) / 5) * index; ctx.beginPath(); ctx.moveTo(x, chartArea.top); ctx.lineTo(x, chartArea.bottom); ctx.stroke(); const time = appState.chart.domainStart + (domainSpan / 5) * index; ctx.fillText(formatDate(new Date(time)), Math.max(chartArea.left, x - 28), height - 10); } }
    function showTooltip(point) { appState.chart.hoverPoint = point; chartTooltipEl.innerHTML = `<strong>${escapeHtml(point.label)}</strong><br>${escapeHtml(formatDate(point.date))}<br>Value: ${escapeHtml(String(point.value))}`; chartTooltipEl.style.left = `${point.x}px`; chartTooltipEl.style.top = `${point.y}px`; chartTooltipEl.classList.add("visible"); }
    function hideTooltip() { appState.chart.hoverPoint = null; chartTooltipEl.classList.remove("visible"); }
    function renderChart() { resizeCanvas(); const width = chartFrameEl.clientWidth; const height = chartFrameEl.clientHeight; chartContext.clearRect(0, 0, width, height); const preparedPoints = getPreparedPoints(); if (!ensureChartDomain(preparedPoints)) { chartEmptyEl.style.display = "block"; chartFrameEl.style.display = "none"; chartSummaryEl.textContent = "Add water test entries with numeric values to build the chart."; hideTooltip(); return; } chartEmptyEl.style.display = "none"; chartFrameEl.style.display = "block"; const activeMetrics = METRICS.filter((metric) => appState.activeMetrics.has(metric.key)); const visiblePoints = preparedPoints.filter((point) => point.timestamp >= appState.chart.domainStart && point.timestamp <= appState.chart.domainEnd); const visibleValues = []; visiblePoints.forEach((point) => activeMetrics.forEach((metric) => { const value = point.values[metric.key]; if (value != null) visibleValues.push(value); })); if (!activeMetrics.length || !visibleValues.length) { chartContext.fillStyle = getComputedStyle(document.body).getPropertyValue("--muted").trim(); chartContext.font = '14px "Segoe UI", sans-serif'; chartContext.fillText("Select at least one metric with numeric readings to draw the chart.", 20, 40); chartSummaryEl.textContent = `${preparedPoints.length} water tests available in the selected ${appState.chartRange} range.`; hideTooltip(); return; } const minY = Math.min(...visibleValues); const maxY = Math.max(...visibleValues); const padding = Math.max((maxY - minY) * 0.15, maxY === minY ? Math.max(Math.abs(maxY) * 0.15, 1) : 0.5); const paddedMinY = minY - padding; const paddedMaxY = maxY + padding; const chartArea = { left: 54, right: width - 16, top: 16, bottom: height - 38 }; const domainSpan = appState.chart.domainEnd - appState.chart.domainStart || 1; const ySpan = paddedMaxY - paddedMinY || 1; drawChartGrid(chartContext, width, height, chartArea, paddedMinY, paddedMaxY); appState.chart.pixelPoints = []; activeMetrics.forEach((metric) => { const color = getMetricColor(metric.key); const points = visiblePoints.filter((point) => point.values[metric.key] != null).map((point) => { const x = chartArea.left + ((point.timestamp - appState.chart.domainStart) / domainSpan) * (chartArea.right - chartArea.left); const y = chartArea.bottom - ((point.values[metric.key] - paddedMinY) / ySpan) * (chartArea.bottom - chartArea.top); const item = { metric: metric.key, label: metric.label, color, x, y, value: point.values[metric.key], date: point.date, entryId: point.id }; appState.chart.pixelPoints.push(item); return item; }); if (!points.length) return; chartContext.strokeStyle = color; chartContext.lineWidth = 2; chartContext.beginPath(); points.forEach((point, index) => { if (index === 0) chartContext.moveTo(point.x, point.y); else chartContext.lineTo(point.x, point.y); }); chartContext.stroke(); points.forEach((point) => { chartContext.fillStyle = color; chartContext.beginPath(); chartContext.arc(point.x, point.y, 4, 0, Math.PI * 2); chartContext.fill(); }); }); chartSummaryEl.textContent = `${visiblePoints.length} water tests shown across the last ${appState.chartRange}. ${activeMetrics.length} metric${activeMetrics.length === 1 ? "" : "s"} active.`; }
    function updateHover(event) { if (!appState.chart.pixelPoints.length) { hideTooltip(); return; } const bounds = chartFrameEl.getBoundingClientRect(); const x = event.clientX - bounds.left; const y = event.clientY - bounds.top; let nearest = null; let distance = 18; appState.chart.pixelPoints.forEach((point) => { const next = Math.hypot(point.x - x, point.y - y); if (next < distance) { distance = next; nearest = point; } }); if (nearest) showTooltip(nearest); else hideTooltip(); }
    function zoomChart(event) { if (!appState.chart.preparedPoints.length) return; event.preventDefault(); const bounds = chartFrameEl.getBoundingClientRect(); const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left - 54) / Math.max(1, bounds.width - 70))); const currentSpan = appState.chart.domainEnd - appState.chart.domainStart; const zoomFactor = event.deltaY < 0 ? 0.85 : 1.18; const totalSpan = appState.chart.maxTime - appState.chart.minTime; const newSpan = Math.max(86400000, Math.min(totalSpan, currentSpan * zoomFactor)); const focusTime = appState.chart.domainStart + currentSpan * ratio; const next = clampDomain(focusTime - newSpan * ratio, focusTime - newSpan * ratio + newSpan, appState.chart.minTime, appState.chart.maxTime); appState.chart.domainStart = next.start; appState.chart.domainEnd = next.end; renderChart(); }
    async function loadEntries() { const response = await fetch("/api/entries"); const entries = await response.json(); appState.entries = entries; renderFeed(entries); resetChartDomain(getPreparedPoints()); if (appState.currentView === "chart") renderChart(); }
    async function loadProducts() { const response = await fetch("/api/products-catalog"); const products = await response.json(); appState.products = products; populateProductSelect(); renderProductsCatalog(); }
    async function loadAppData() { await Promise.all([loadEntries(), loadProducts()]); }
    async function deleteEntry(entryId) { setStatus("Removing entry..."); try { const response = await fetch("/api/entries/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: entryId }) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to remove entry."); setStatus("Entry removed.", "success"); await loadEntries(); } catch (error) { setStatus(error.message || "Unable to remove entry.", "error"); } }
    async function submitJsonForm(form, url) { const submitButton = form.querySelector("button[type='submit']"); submitButton.disabled = true; setStatus("Saving entry..."); try { const payload = Object.fromEntries(new FormData(form).entries()); const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to save entry."); form.reset(); setTodayDefaults(); populateProductSelect(); setStatus("Entry saved.", "success"); await loadEntries(); } catch (error) { setStatus(error.message || "Unable to save entry.", "error"); } finally { submitButton.disabled = false; } }
    async function submitPhotoForm(form) { const submitButton = form.querySelector("button[type='submit']"); submitButton.disabled = true; setStatus("Uploading photo..."); try { const response = await fetch("/api/photos", { method: "POST", body: new FormData(form) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to save photo entry."); form.reset(); setTodayDefaults(); setStatus("Photo entry saved.", "success"); await loadEntries(); } catch (error) { setStatus(error.message || "Unable to save photo entry.", "error"); } finally { submitButton.disabled = false; } }
    async function addCatalogProduct(name) { const response = await fetch("/api/products-catalog/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to add product."); return result; }
    async function renameCatalogProduct(id, name) { const response = await fetch("/api/products-catalog/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, name }) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to rename product."); return result; }
    async function removeCatalogProduct(id) { const response = await fetch("/api/products-catalog/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || "Unable to remove product."); return result; }
    tabs.forEach((tab) => tab.addEventListener("click", () => activateMode(tab.dataset.mode)));
    viewButtons.forEach((button) => button.addEventListener("click", () => activateMainView(button.dataset.view)));
    rangeButtons.forEach((button) => button.addEventListener("click", () => { appState.chartRange = button.dataset.range; rangeButtons.forEach((item) => item.classList.toggle("active", item === button)); resetChartDomain(getPreparedPoints()); renderChart(); }));
    metricChoicesEl.addEventListener("change", (event) => { const input = event.target.closest("input[data-metric]"); if (!input) return; if (input.checked) appState.activeMetrics.add(input.dataset.metric); else appState.activeMetrics.delete(input.dataset.metric); renderChart(); });
    resetZoomButton.addEventListener("click", () => { resetChartDomain(getPreparedPoints()); renderChart(); });
    chartFrameEl.addEventListener("wheel", zoomChart, { passive: false });
    chartFrameEl.addEventListener("pointerdown", (event) => { if (!appState.chart.preparedPoints.length) return; appState.chart.dragging = true; chartFrameEl.classList.add("dragging"); chartFrameEl.setPointerCapture(event.pointerId); appState.chart.dragStartX = event.clientX; appState.chart.dragDomainStart = appState.chart.domainStart; appState.chart.dragDomainEnd = appState.chart.domainEnd; });
    chartFrameEl.addEventListener("pointermove", (event) => { if (appState.chart.dragging) { const bounds = chartFrameEl.getBoundingClientRect(); const span = appState.chart.dragDomainEnd - appState.chart.dragDomainStart; const shift = ((event.clientX - appState.chart.dragStartX) / Math.max(1, bounds.width - 70)) * span; const next = clampDomain(appState.chart.dragDomainStart - shift, appState.chart.dragDomainEnd - shift, appState.chart.minTime, appState.chart.maxTime); appState.chart.domainStart = next.start; appState.chart.domainEnd = next.end; renderChart(); return; } updateHover(event); });
    chartFrameEl.addEventListener("pointerup", () => { appState.chart.dragging = false; chartFrameEl.classList.remove("dragging"); });
    chartFrameEl.addEventListener("pointerleave", () => { if (!appState.chart.dragging) hideTooltip(); });
    feedEl.addEventListener("click", (event) => { const button = event.target.closest(".entry-delete"); if (!button) return; const entryId = Number(button.dataset.entryId); if (!entryId || !window.confirm("Remove this entry?")) return; deleteEntry(entryId); });
    productsListEl.addEventListener("click", async (event) => { const row = event.target.closest(".product-row"); if (!row) return; const productId = Number(row.dataset.productId); const input = row.querySelector(".product-name-input"); if (event.target.closest(".product-rename")) { setStatus("Saving product..."); try { await renameCatalogProduct(productId, input.value); setStatus("Product updated.", "success"); await loadAppData(); } catch (error) { setStatus(error.message || "Unable to rename product.", "error"); } return; } if (event.target.closest(".product-remove")) { if (!window.confirm("Remove this product from the list?")) return; setStatus("Removing product..."); try { await removeCatalogProduct(productId); setStatus("Product removed from the list.", "success"); await loadProducts(); } catch (error) { setStatus(error.message || "Unable to remove product.", "error"); } } });
    document.getElementById("product-catalog-form").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; const input = document.getElementById("new-product-name"); const submit = form.querySelector("button"); submit.disabled = true; setStatus("Adding product..."); try { await addCatalogProduct(input.value); form.reset(); setStatus("Product added.", "success"); await loadProducts(); } catch (error) { setStatus(error.message || "Unable to add product.", "error"); } finally { submit.disabled = false; } });
    document.getElementById("water-form").addEventListener("submit", (event) => { event.preventDefault(); submitJsonForm(event.currentTarget, "/api/water-tests"); });
    document.getElementById("product-form").addEventListener("submit", (event) => { event.preventDefault(); submitJsonForm(event.currentTarget, "/api/products"); });
    document.getElementById("photo-form").addEventListener("submit", (event) => { event.preventDefault(); submitPhotoForm(event.currentTarget); });
    window.addEventListener("resize", () => { if (appState.currentView === "chart") renderChart(); });
    setTodayDefaults();
    renderMetricChoices();
    activateMode(APP_DEFAULT_MODE);
    activateMainView("log");
    loadAppData().catch(() => { setStatus("Unable to load app data.", "error"); });
  </script>
</body>
</html>
"""

    body_class = "theme-black" if options.get("black_mode") else ""
    return template.replace("__DEFAULT_MODE__", options["default_mode"]).replace("__BODY_CLASS__", body_class)


class PondDiaryHandler(BaseHTTPRequestHandler):
    server_version = "PondDiary/1.4"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, render_app(load_options()))
            return
        if parsed.path == "/api/entries":
            json_response(self, fetch_entries())
            return
        if parsed.path == "/api/products-catalog":
            json_response(self, fetch_products())
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
                details = {"ph": normalize_text(payload.get("ph", "")), "temperature": normalize_text(payload.get("temperature", "")), "ammonia": normalize_text(payload.get("ammonia", "")), "nitrite": normalize_text(payload.get("nitrite", "")), "nitrate": normalize_text(payload.get("nitrate", "")), "hardness": normalize_text(payload.get("hardness", ""))}
                insert_entry("water_test", "Water test recorded", notes, event_date, details)
                json_response(self, {"status": "ok"}, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/products":
                payload = parse_json(self)
                event_date = validate_event_date(payload.get("eventDate", ""))
                try:
                    product_id = int(payload.get("productId", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("A valid product is required.") from exc
                product_name = get_product_name(product_id)
                if not product_name:
                    raise ValueError("A valid product is required.")
                notes = normalize_text(payload.get("notes", ""))
                details = {"dose": normalize_text(payload.get("dose", "")), "purpose": normalize_text(payload.get("purpose", "")), "productId": product_id}
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
                insert_entry("photo", "Pond photo", description, event_date, {"filename": filename}, photo_path=filename)
                json_response(self, {"status": "ok"}, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/products-catalog/add":
                payload = parse_json(self)
                json_response(self, add_product(payload.get("name", "")), HTTPStatus.CREATED)
                return
            if parsed.path == "/api/products-catalog/rename":
                payload = parse_json(self)
                json_response(self, rename_product(int(payload.get("id", 0)), payload.get("name", "")), HTTPStatus.OK)
                return
            if parsed.path == "/api/products-catalog/delete":
                payload = parse_json(self)
                if not remove_product(int(payload.get("id", 0))):
                    json_response(self, {"error": "Product not found."}, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, {"status": "ok"}, HTTPStatus.OK)
                return
            if parsed.path == "/api/entries/delete":
                payload = parse_json(self)
                if not delete_entry(int(payload.get("id", 0))):
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
