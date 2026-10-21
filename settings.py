"""
Settings module.

Everything a shop owner can change after the first-time setup lives here:

  V1  Business information, invoice settings, account / password,
      stock + billing options, backup & restore
  V2  Printing, GST / tax, security, preferences

Settings are stored in the single `business` row (id = 1), so they are
included automatically in every backup of the database file.
"""

import base64
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from io import BytesIO

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash

from database import DATA_DIR, DB_PATH, get_connection, initialize_database


settings_bp = Blueprint("settings", __name__)


ALL_PAYMENT_METHODS = ["Cash", "UPI", "Card", "Credit"]
PAPER_SIZES = {
    "A4": "A4 (standard sheet)",
    "A5": "A5 (half sheet)",
    "Thermal80": "Thermal receipt - 80 mm",
    "Thermal58": "Thermal receipt - 58 mm",
}
DATE_FORMATS = {
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
}
CURRENCY_SYMBOLS = ["₹", "$", "€", "£", "Rs.", "AED", "Tk"]

MAX_LOGO_BYTES = 512 * 1024
MAX_RESTORE_BYTES = 200 * 1024 * 1024
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
KEEP_SAFETY_BACKUPS = 10

GSTIN_PATTERN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^[0-9+\-\s()]{7,20}$")
PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9\-]{1,10}$")


# ==================================================
# DEFAULTS + HELPERS
# ==================================================

DEFAULTS = {
    "store_name": "",
    "address": "",
    "city": "",
    "state": "",
    "postal_code": "",
    "phone": "",
    "email": "",
    "gstin": "",
    "invoice_prefix": "INV",
    "invoice_start_number": 1,
    "invoice_footer": "",
    "logo": "",
    "show_logo": 1,
    "default_low_stock": 10.0,
    "payment_methods": ",".join(ALL_PAYMENT_METHODS),
    "require_customer_name": 1,
    "paper_size": "A4",
    "auto_print": 0,
    "gst_enabled": 0,
    "gst_rate": 18.0,
    "session_timeout": 480,
    "currency_symbol": "₹",
    "date_format": "DD-MM-YYYY",
}


def get_settings(conn=None, store_id=None):
    """Return every setting as a dict, with defaults filled in."""

    own = conn is None
    if own:
        conn = get_connection()

    try:
        sid = store_id or session.get("store_id")
        if sid:
            row = conn.execute("SELECT * FROM business WHERE store_id = ?", (sid,)).fetchone()
            if not row:
                row = conn.execute("SELECT * FROM business ORDER BY id ASC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM business ORDER BY id ASC LIMIT 1").fetchone()
    finally:
        if own:
            conn.close()

    settings = dict(DEFAULTS)

    if row:
        for key in row.keys():
            value = row[key]
            if value is not None and key in DEFAULTS:
                settings[key] = value

    settings["payment_methods_list"] = enabled_payment_methods(settings)
    return settings


def enabled_payment_methods(settings):
    chosen = [
        m.strip()
        for m in str(settings.get("payment_methods") or "").split(",")
    ]
    chosen = [m for m in ALL_PAYMENT_METHODS if m in chosen]
    return chosen or ["Cash"]


def _guard():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _bad(message, status=400):
    return jsonify({"error": message}), status


def _text(data, key, limit=200):
    return " ".join(str(data.get(key) or "").split())[:limit]


def _flag(data, key):
    return 1 if data.get(key) in (True, 1, "1", "true", "on") else 0


def _update_business(fields, store_id=None):
    """Update columns of the business row for this store. Keys are code-controlled."""

    sid = store_id or session.get("store_id")
    conn = get_connection()
    try:
        if sid:
            existing = conn.execute("SELECT id FROM business WHERE store_id = ?", (sid,)).fetchone()
            if not existing:
                conn.execute("INSERT INTO business (store_id) VALUES (?)", (sid,))
            names = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE business SET {names} WHERE store_id = ?",
                list(fields.values()) + [sid],
            )
        else:
            conn.execute("INSERT OR IGNORE INTO business (id) VALUES (1)")
            names = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE business SET {names} WHERE id = 1",
                list(fields.values()),
            )
        conn.commit()
    finally:
        conn.close()


def format_datetime(value, settings=None):
    """Bills are stored in UTC. Show them in local time using the chosen format."""

    if not value:
        return ""

    settings = settings or get_settings()
    pattern = DATE_FORMATS.get(settings["date_format"], "%d-%m-%Y")

    try:
        moment = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        from datetime import timezone

        local = moment.replace(tzinfo=timezone.utc).astimezone()
        return local.strftime(pattern + " %I:%M %p")
    except ValueError:
        return str(value)


# ==================================================
# PAGE
# ==================================================

@settings_bp.route("/settings")
def settings_page():

    if not session.get("logged_in"):
        return redirect("/")

    if session.get("role") == "staff":
        return redirect("/billing")

    store_id = session.get("store_id")
    conn = get_connection()
    try:
        bill_count = conn.execute(
            "SELECT COUNT(*) FROM bills WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))",
            (store_id, store_id)
        ).fetchone()[0]
        store = conn.execute("SELECT store_code, store_name, plan_status, trial_ends_at FROM stores WHERE id = ?", (store_id,)).fetchone()
    finally:
        conn.close()

    store_code = store["store_code"] if store else (session.get("store_code") or "")
    store_name = store["store_name"] if store else (session.get("store_name") or "")

    return render_template(
        "settings.html",
        paper_sizes=PAPER_SIZES,
        date_formats=list(DATE_FORMATS.keys()),
        currency_symbols=CURRENCY_SYMBOLS,
        all_payment_methods=ALL_PAYMENT_METHODS,
        has_bills=bill_count > 0,
        database_file=os.path.basename(DB_PATH),
        store_code=store_code,
        store_name=store_name,
        plan_status=store["plan_status"] if store else "trial",
        trial_ends_at=store["trial_ends_at"] if store else ""
    )


# ==================================================
# READ
# ==================================================

@settings_bp.route("/api/settings", methods=["GET"])
def read_settings():

    denied = _guard()
    if denied:
        return denied

    store_id = session.get("store_id")
    settings = get_settings(store_id=store_id)

    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT username, owner_name FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone()
        bill_count = conn.execute(
            "SELECT COUNT(*) FROM bills WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))",
            (store_id, store_id)
        ).fetchone()[0]
        store = conn.execute("SELECT store_code, store_name, plan_status, trial_ends_at FROM stores WHERE id = ?", (store_id,)).fetchone()
    finally:
        conn.close()

    settings["username"] = user["username"] if user else ""
    settings["owner_name"] = (user["owner_name"] or "") if user else ""
    settings["has_bills"] = bill_count > 0
    settings["has_logo"] = bool(settings.get("logo"))
    settings["logo"] = settings.get("logo") or ""
    settings["store_code"] = store["store_code"] if store else (session.get("store_code") or "")
    settings["store_name"] = settings.get("store_name") or (store["store_name"] if store else (session.get("store_name") or ""))
    settings["plan_status"] = store["plan_status"] if store else "trial"
    settings["trial_ends_at"] = store["trial_ends_at"] if store else ""

    return jsonify(settings)


# ==================================================
# V1 - BUSINESS INFORMATION
# ==================================================

@settings_bp.route("/api/settings/business", methods=["PUT"])
def save_business():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    store_name = _text(data, "store_name", 100)
    address = _text(data, "address", 250)
    city = _text(data, "city", 80)
    state = _text(data, "state", 80)
    postal_code = _text(data, "postal_code", 12)
    phone = _text(data, "phone", 20)
    email = _text(data, "email", 120)
    gstin = _text(data, "gstin", 15).upper().replace(" ", "")

    required = [
        (store_name, "Business / Store name"),
        (address, "Address"),
        (city, "City"),
        (state, "State"),
        (postal_code, "PIN / Postal code"),
        (phone, "Phone number"),
    ]

    for value, label in required:
        if not value:
            return _bad(f"{label} is required")

    if not PHONE_PATTERN.match(phone):
        return _bad("Phone number looks invalid")

    if email and not EMAIL_PATTERN.match(email):
        return _bad("Email address looks invalid")

    if gstin and not GSTIN_PATTERN.match(gstin):
        return _bad(
            "GSTIN must be 15 characters, for example 33ABCDE1234F1Z5"
        )

    _update_business({
        "store_name": store_name,
        "address": address,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "phone": phone,
        "email": email,
        "gstin": gstin,
    })

    return jsonify({"message": "Business information saved"})


# ==================================================
# V1 - INVOICE SETTINGS (+ logo)
# ==================================================

@settings_bp.route("/api/settings/invoice", methods=["PUT"])
def save_invoice():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    prefix = _text(data, "invoice_prefix", 10).upper()
    footer = str(data.get("invoice_footer") or "").strip()[:300]

    if not prefix:
        prefix = "INV"

    if not PREFIX_PATTERN.match(prefix):
        return _bad("Invoice prefix may only contain letters, numbers and -")

    fields = {
        "invoice_prefix": prefix,
        "invoice_footer": footer,
        "show_logo": _flag(data, "show_logo"),
    }

    # The starting number can only change before the first bill exists,
    # otherwise new bill numbers could clash with old ones.
    conn = get_connection()
    try:
        bill_count = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    finally:
        conn.close()

    if bill_count == 0 and "invoice_start_number" in data:
        try:
            start = int(data.get("invoice_start_number"))
        except (TypeError, ValueError):
            return _bad("Invoice starting number must be a number")

        if start < 1 or start > 99999999:
            return _bad("Invoice starting number must be 1 or more")

        fields["invoice_start_number"] = start

    _update_business(fields)

    return jsonify({"message": "Invoice settings saved"})


def _detect_image(blob):
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


@settings_bp.route("/api/settings/logo", methods=["POST"])
def upload_logo():

    denied = _guard()
    if denied:
        return denied

    upload = request.files.get("logo")

    if not upload or not upload.filename:
        return _bad("Please choose an image file")

    blob = upload.read(MAX_LOGO_BYTES + 1)

    if len(blob) > MAX_LOGO_BYTES:
        return _bad("Logo is too large. Please use an image under 500 KB")

    mime = _detect_image(blob)

    if not mime:
        return _bad("Logo must be a PNG, JPG or WebP image")

    data_uri = (
        f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
    )

    _update_business({"logo": data_uri})

    return jsonify({"message": "Logo uploaded", "logo": data_uri})


@settings_bp.route("/api/settings/logo", methods=["DELETE"])
def remove_logo():

    denied = _guard()
    if denied:
        return denied

    _update_business({"logo": ""})

    return jsonify({"message": "Logo removed"})


# ==================================================
# V1 - ACCOUNT
# ==================================================

@settings_bp.route("/api/settings/account", methods=["PUT"])
def save_account():
    """Owner name + username. Requires the current password."""

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    owner_name = _text(data, "owner_name", 80)
    username = _text(data, "username", 40)
    current_password = str(data.get("current_password") or "")

    if not owner_name:
        return _bad("Owner name is required")

    if len(username) < 3 or " " in username:
        return _bad("Username must be at least 3 characters, with no spaces")

    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT id, password FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if not user or not check_password_hash(
            user["password"], current_password
        ):
            return _bad("Current password is incorrect")

        clash = conn.execute(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            (username, user["id"]),
        ).fetchone()

        if clash:
            return _bad("That username is already taken")

        conn.execute(
            "UPDATE users SET username = ?, owner_name = ? WHERE id = ?",
            (username, owner_name, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    session["username"] = username
    session["owner_name"] = owner_name

    return jsonify({
        "message": "Account details saved",
        "owner_name": owner_name,
        "username": username,
    })


@settings_bp.route("/api/settings/password", methods=["PUT"])
def change_password():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    current_password = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")
    confirm = str(data.get("confirm_password") or "")

    if len(new_password) < 6:
        return _bad("New password must be at least 6 characters")

    if new_password != confirm:
        return _bad("New passwords do not match")

    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT id, password FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if not user or not check_password_hash(
            user["password"], current_password
        ):
            return _bad("Current password is incorrect")

        if check_password_hash(user["password"], new_password):
            return _bad("New password must be different from the current one")

        conn.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"message": "Password changed"})


# ==================================================
# V1 - STOCK + BILLING OPTIONS
# ==================================================

@settings_bp.route("/api/settings/billing", methods=["PUT"])
def save_billing():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    try:
        low_stock = float(data.get("default_low_stock"))
    except (TypeError, ValueError):
        return _bad("Low-stock level must be a number")

    if low_stock < 0 or low_stock > 1000000:
        return _bad("Low-stock level must be 0 or more")

    methods = data.get("payment_methods")

    if not isinstance(methods, list):
        return _bad("Choose at least one payment method")

    methods = [m for m in ALL_PAYMENT_METHODS if m in methods]

    if not methods:
        return _bad("Choose at least one payment method")

    _update_business({
        "default_low_stock": low_stock,
        "payment_methods": ",".join(methods),
        "require_customer_name": _flag(data, "require_customer_name"),
    })

    return jsonify({"message": "Stock and billing options saved"})


# ==================================================
# V2 - PRINTING
# ==================================================

@settings_bp.route("/api/settings/printing", methods=["PUT"])
def save_printing():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    paper = str(data.get("paper_size") or "")

    if paper not in PAPER_SIZES:
        return _bad("Choose a paper size from the list")

    _update_business({
        "paper_size": paper,
        "auto_print": _flag(data, "auto_print"),
    })

    return jsonify({"message": "Printing settings saved"})


# ==================================================
# V2 - GST / TAX
# ==================================================

@settings_bp.route("/api/settings/gst", methods=["PUT"])
def save_gst():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    enabled = _flag(data, "gst_enabled")

    try:
        rate = float(data.get("gst_rate"))
    except (TypeError, ValueError):
        return _bad("GST rate must be a number")

    if rate < 0 or rate > 50:
        return _bad("GST rate must be between 0 and 50")

    if enabled:
        gstin = get_settings()["gstin"]
        if not gstin:
            return _bad(
                "Add your GSTIN under Business Information before "
                "turning on GST"
            )

    _update_business({
        "gst_enabled": enabled,
        "gst_rate": round(rate, 2),
    })

    return jsonify({"message": "GST settings saved"})


# ==================================================
# V2 - SECURITY
# ==================================================

@settings_bp.route("/api/settings/security", methods=["PUT"])
def save_security():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    try:
        minutes = int(data.get("session_timeout"))
    except (TypeError, ValueError):
        return _bad("Auto-logout time must be a number of minutes")

    if minutes != 0 and not (5 <= minutes <= 1440):
        return _bad("Auto-logout must be between 5 and 1440 minutes (or 0 for never)")

    _update_business({"session_timeout": minutes})

    return jsonify({"message": "Security settings saved"})


# ==================================================
# V2 - PREFERENCES
# ==================================================

@settings_bp.route("/api/settings/preferences", methods=["PUT"])
def save_preferences():

    denied = _guard()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}

    symbol = _text(data, "currency_symbol", 5)
    date_format = str(data.get("date_format") or "")

    if not symbol:
        return _bad("Currency symbol is required")

    if date_format not in DATE_FORMATS:
        return _bad("Choose a date format from the list")

    _update_business({
        "currency_symbol": symbol,
        "date_format": date_format,
    })

    return jsonify({"message": "Preferences saved"})


# ==================================================
# V1 - BACKUP & RESTORE
# ==================================================

REQUIRED_TABLES = {"users", "business", "bills", "bill_items", "products"}


@settings_bp.route("/api/backup/download")
def download_backup():

    denied = _guard()
    if denied:
        return denied

    handle, temp_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)

    try:
        # sqlite's own backup API gives a consistent copy even while
        # the software is running.
        source = sqlite3.connect(DB_PATH)
        target = sqlite3.connect(temp_path)

        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        with open(temp_path, "rb") as file:
            payload = BytesIO(file.read())
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    name = "billing-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"

    return send_file(
        payload,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=name,
    )


def _validate_backup_file(path):
    """Return an error message, or None when the file is a usable backup."""

    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return "That file is not a valid backup"

    try:
        try:
            check = probe.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            return "That file is not a valid backup"

        if not check or check[0] != "ok":
            return "That backup file is damaged"

        tables = {
            row[0]
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        if not REQUIRED_TABLES.issubset(tables):
            return "That file is not a backup from this billing software"

        user_count = probe.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        done = probe.execute(
            "SELECT COUNT(*) FROM business WHERE setup_completed = 1"
        ).fetchone()[0]

        if user_count < 1 or done < 1:
            return "That backup has no completed setup or owner account"

    except sqlite3.Error:
        return "That file is not a valid backup"
    finally:
        probe.close()

    return None


def _prune_safety_backups():
    try:
        files = sorted(
            f for f in os.listdir(BACKUP_DIR) if f.startswith("before-restore-")
        )
        for old in files[:-KEEP_SAFETY_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass


@settings_bp.route("/api/backup/restore", methods=["POST"])
def restore_backup():

    denied = _guard()
    if denied:
        return denied

    if (request.form.get("confirm") or "").strip().upper() != "RESTORE":
        return _bad("Type RESTORE to confirm")

    upload = request.files.get("backup")

    if not upload or not upload.filename:
        return _bad("Please choose a backup file")

    handle, temp_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)

    try:
        blob = upload.read(MAX_RESTORE_BYTES + 1)

        if len(blob) > MAX_RESTORE_BYTES:
            return _bad("Backup file is too large")

        if not blob.startswith(b"SQLite format 3\x00"):
            return _bad("That file is not a valid backup")

        with open(temp_path, "wb") as file:
            file.write(blob)

        problem = _validate_backup_file(temp_path)

        if problem:
            return _bad(problem)

        # Safety copy of what is there now, so a wrong restore can be undone.
        os.makedirs(BACKUP_DIR, exist_ok=True)

        safety_name = (
            "before-restore-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".db"
        )

        live = sqlite3.connect(DB_PATH)
        safety = sqlite3.connect(os.path.join(BACKUP_DIR, safety_name))

        try:
            live.backup(safety)
        finally:
            safety.close()
            live.close()

        # Copy the uploaded database over the live one.
        source = sqlite3.connect(temp_path)
        target = sqlite3.connect(DB_PATH)

        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        # Bring a backup made by an older version up to date.
        initialize_database()
        _prune_safety_backups()

    except sqlite3.Error as error:
        return _bad(f"Restore failed: {error}", 500)

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    # The restored data has its own users, so sign in again.
    session.clear()

    return jsonify({
        "message": "Backup restored. Please log in again.",
        "safety_copy": safety_name,
    })
