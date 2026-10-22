import os
import json
import secrets
import time
from datetime import datetime, date
from functools import wraps

import razorpay
from flask import Flask, render_template, request, redirect, session, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash

from paths import RESOURCE_DIR
from database import (
    DATA_DIR, get_connection, initialize_database, register_new_store,
    get_store_subscription, generate_license_key, redeem_license_key, process_test_payment,
    record_payment_order, record_payment_success
)
from invoice import get_invoice_data
from reports import reports_bp
from settings import settings_bp, get_settings, format_datetime
from founder import founder_bp


app = Flask(
    __name__,
    template_folder=os.path.join(RESOURCE_DIR, "templates"),
    static_folder=os.path.join(RESOURCE_DIR, "static")
)
app.register_blueprint(reports_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(founder_bp)


# ==================================================
# SECRET KEY
# ==================================================
# The key that signs login sessions must be private and unique to each
# installation. It comes from the BILLING_SECRET_KEY environment variable,
# or is generated once and kept in data/secret.key.

def load_secret_key():

    from_env = os.environ.get("BILLING_SECRET_KEY")

    if from_env:
        return from_env

    os.makedirs(DATA_DIR, exist_ok=True)

    key_file = os.path.join(DATA_DIR, "secret.key")

    if os.path.exists(key_file):

        with open(key_file, "r", encoding="utf-8") as file:
            existing = file.read().strip()

        if existing:
            return existing

    new_key = secrets.token_hex(32)

    with open(key_file, "w", encoding="utf-8") as file:
        file.write(new_key)

    return new_key


app.secret_key = load_secret_key()

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=210 * 1024 * 1024,
)


# ==================================================
# SHARED TEMPLATE VALUES
# ==================================================
# Every page can use {{ store_name }}, so the software shows the
# owner's own business name instead of a fixed product name.

APP_VERSION = "2.5"

@app.context_processor
def inject_branding():

    base_data = {
        "software_name": "Billora",
        "brand_name": "Billora",
        "store_name": "Store",
        "store_logo": "",
        "app_version": f"v{APP_VERSION}",
        "raw_version": APP_VERSION,
        "sub_status": "trial",
        "sub_active": True,
        "sub_time_left": "",
        "sub_code": "",
        "business_category": session.get("business_category", ""),
    }

    try:
        settings = get_settings()
        base_data["store_name"] = settings["store_name"] or "Billing"
        base_data["store_logo"] = settings["logo"] if settings["show_logo"] else ""
    except Exception:
        pass

    store_id = session.get("store_id")
    if store_id:
        try:
            sub = get_store_subscription(store_id)
            base_data["sub_status"] = sub.get("plan_status", "trial")
            base_data["sub_active"] = sub.get("is_active", True)
            base_data["sub_time_left"] = sub.get("time_left_str", "")
            base_data["sub_code"] = sub.get("store_code", "")
            if not base_data["business_category"]:
                base_data["business_category"] = sub.get("business_category", "")
        except Exception:
            pass

    return base_data


@app.template_filter("local_datetime")
def local_datetime_filter(value):
    return format_datetime(value)


# ==================================================
# SUBSCRIPTION & TRIAL LOCKOUT GATEWAY
# ==================================================

@app.before_request
def check_subscription_expiration():
    if not session.get("logged_in") or not session.get("store_id"):
        return None

    path = request.path
    # Essential routes that must always be accessible
    if (
        path.startswith("/subscription") or
        path.startswith("/about") or
        path.startswith("/terms") or
        path.startswith("/privacy") or
        path.startswith("/refund") or
        path.startswith("/static") or
        path.startswith("/logout") or
        path.startswith("/api/subscription") or
        path.startswith("/api/payment") or
        path.startswith("/founder") or
        path in ["/", "/login", "/register", "/staff-login"]
    ):
        return None

    # Check store subscription status
    try:
        sub = get_store_subscription(session.get("store_id"))
        if not sub.get("is_active", True):
            if path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "error": "Your trial or subscription has expired. Please activate Pro (₹5 for 30 mins) or contact founder Dheena (+91 7502399148).",
                    "expired": True
                }), 403
            return redirect("/subscription?expired=1")
    except Exception as e:
        print("Subscription check error:", e)

    return None


# ==================================================
# AUTO LOGOUT (Settings -> Security)
# ==================================================

@app.before_request
def enforce_session_timeout():

    if request.path.startswith("/static") or request.path.startswith("/founder") or not session.get("logged_in"):
        return None

    try:
        minutes = int(get_settings()["session_timeout"] or 0)
    except Exception:
        minutes = 0

    now = int(time.time())
    last = session.get("last_active", now)

    if minutes and now - last > minutes * 60:

        session.clear()

        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401

        return redirect("/")

    session["last_active"] = now

    return None


# ==================================================
# LOGIN ATTEMPT LIMIT
# ==================================================
# 5 wrong passwords from the same address -> locked for 5 minutes.

FAILED_LOGINS = {}
MAX_FAILED_LOGINS = 5
LOCK_SECONDS = 300


def login_locked_seconds(address):

    record = FAILED_LOGINS.get(address)

    if not record:
        return 0

    count, since = record
    remaining = LOCK_SECONDS - (time.time() - since)

    if remaining <= 0:
        FAILED_LOGINS.pop(address, None)
        return 0

    return int(remaining) + 1 if count >= MAX_FAILED_LOGINS else 0


# ==================================================
# ROLE-BASED ACCESS CONTROL
# ==================================================

def require_role(*allowed_roles):
    """Decorator: allow only the given roles, otherwise 403."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect("/")
            if session.get("role") not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


@app.errorhandler(403)
def forbidden_page(e):
    role = session.get("role", "")
    if role == "staff":
        return redirect("/billing")
    return "Access Denied", 403


# ==================================================
# SETUP CHECK
# ==================================================

def is_setup_complete():

    conn = get_connection()

    row = conn.execute("""
        SELECT setup_completed
        FROM business
        WHERE id = 1
    """).fetchone()

    conn.close()

    return bool(row and row["setup_completed"])


# ==================================================
# CLOUD SESSION & SECURITY
# ==================================================

@app.before_request
def check_cloud_session():
    # Public routes that don't need login
    public_routes = [
        "/", "/login", "/register", "/api/register",
        "/about", "/terms", "/privacy", "/refund", "/refund-policy"
    ]
    if request.path.startswith("/static") or request.path.startswith("/founder") or request.path in public_routes:
        return

    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")


# ==================================================
# REGISTER (Cloud Store Signup)
# ==================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("logged_in"):
        if session.get("role") == "staff":
            return redirect("/billing")
        return redirect("/dashboard")

    if request.method == "POST":
        store_name = request.form.get("store_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        email_or_phone = request.form.get("email_or_phone", "").strip()
        store_code = request.form.get("store_code", "").strip().upper()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip()
        address = request.form.get("address", "").strip()
        business_category = request.form.get("business_category", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        form_data = {
            "store_name": store_name,
            "owner_name": owner_name,
            "email_or_phone": email_or_phone,
            "store_code": store_code,
            "city": city,
            "state": state,
            "address": address,
            "business_category": business_category
        }

        if not store_name or not owner_name or not email_or_phone or not password:
            return render_template(
                "register.html",
                error="Please fill in all required fields marked with *.",
                **form_data
            )

        # Validate custom store code if provided
        if store_code:
            import re
            if not re.match(r"^[A-Z0-9\-]{3,20}$", store_code):
                return render_template(
                    "register.html",
                    error="Store code must be 3-20 characters long and contain letters/numbers (e.g. STORE-101).",
                    **form_data
                )
            conn = get_connection()
            code_exists = conn.execute("SELECT id FROM stores WHERE UPPER(store_code) = ?", (store_code,)).fetchone()
            conn.close()
            if code_exists:
                return render_template(
                    "register.html",
                    error="Store code already exists. Please choose another store code.",
                    **form_data
                )

        if len(password) < 4:
            return render_template(
                "register.html",
                error="Password must be at least 4 characters.",
                **form_data
            )

        if password != confirm_password:
            return render_template(
                "register.html",
                error="Passwords do not match.",
                **form_data
            )

        # Check if email/phone already registered
        conn = get_connection()
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ? OR phone = ? OR username = ?",
            (email_or_phone, email_or_phone, email_or_phone)
        ).fetchone()
        conn.close()

        if existing:
            return render_template(
                "register.html",
                error="An account with this email/phone already exists. Please login.",
                **form_data
            )

        try:
            res = register_new_store(
                store_name, owner_name, email_or_phone, password,
                city=city, state=state, address=address, business_type=business_category,
                custom_store_code=store_code
            )
            session.clear()
            session["logged_in"] = True
            session["user_id"] = res["user_id"]
            session["store_id"] = res["store_id"]
            session["store_code"] = res["store_code"]
            session["store_name"] = res["store_name"]
            session["username"] = res["username"]
            session["owner_name"] = res["owner_name"]
            session["business_category"] = res.get("business_category", business_category)
            session["role"] = "admin"
            session["last_active"] = int(time.time())
            return redirect("/dashboard")
        except ValueError as ve:
            return render_template(
                "register.html",
                error=str(ve),
                **form_data
            )
        except Exception as e:
            return render_template(
                "register.html",
                error=f"Registration failed: {str(e)}",
                **form_data
            )

    return render_template("register.html")


# ==================================================
# LANDING PAGE (Marketing & Product Showcase)
# ==================================================

@app.route("/")
def index():
    if session.get("logged_in"):
        if session.get("role") == "staff":
            return redirect("/billing")
        return redirect("/dashboard")
    return render_template("index.html")


# ==================================================
# LOGIN (Unified Owner & Staff)
# ==================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and session.get("logged_in"):
        if session.get("role") == "staff":
            return redirect("/billing")
        return redirect("/dashboard")

    if request.method == "POST":
        login_type = request.form.get("login_type", "owner")
        address = request.remote_addr or "unknown"
        wait = login_locked_seconds(address)

        if wait:
            return render_template(
                "login.html",
                error=f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                login_type=login_type
            )

        conn = get_connection()
        user = None

        if login_type == "staff":
            store_code = request.form.get("store_code", "").strip().upper()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if not store_code or not username or not password:
                conn.close()
                return render_template(
                    "login.html",
                    error="Store Code, Staff Username, and Password are required.",
                    login_type="staff",
                    store_code=store_code,
                    username=username
                )

            user = conn.execute("""
                SELECT u.id, u.store_id, u.username, u.password, u.owner_name, u.role,
                       s.store_name, s.store_code, s.plan_status, s.trial_ends_at
                FROM users u
                JOIN stores s ON s.id = u.store_id
                WHERE UPPER(s.store_code) = ? AND u.username = ?
            """, (store_code, username)).fetchone()
        else:
            # Owner login
            identifier = request.form.get("identifier", "").strip()
            password = request.form.get("password", "")

            if not identifier or not password:
                conn.close()
                return render_template(
                    "login.html",
                    error="Email/Phone/Username and Password are required.",
                    login_type="owner",
                    identifier=identifier
                )

            user = conn.execute("""
                SELECT u.id, u.store_id, u.username, u.password, u.owner_name, u.role,
                       s.store_name, s.store_code, s.plan_status, s.trial_ends_at
                FROM users u
                LEFT JOIN stores s ON s.id = u.store_id
                WHERE u.email = ? OR u.phone = ? OR u.username = ?
            """, (identifier, identifier, identifier)).fetchone()

        conn.close()

        if user and check_password_hash(user["password"], password):
            FAILED_LOGINS.pop(address, None)

            session.clear()
            session["logged_in"] = True
            session["user_id"] = user["id"]
            session["store_id"] = user["store_id"]
            session["store_code"] = user["store_code"] if user["store_code"] else ""
            session["store_name"] = user["store_name"] if user["store_name"] else "My Store"
            session["username"] = user["username"]
            session["owner_name"] = user["owner_name"]
            session["role"] = user["role"] if user["role"] else "admin"
            session["last_active"] = int(time.time())

            if session["role"] == "staff":
                return redirect("/billing")
            return redirect("/dashboard")

        count, since = FAILED_LOGINS.get(address, (0, time.time()))
        FAILED_LOGINS[address] = (count + 1, since)

        error_msg = "Invalid Store Code, Username, or Password" if login_type == "staff" else "Invalid Email/Phone or Password"
        return render_template("login.html", error=error_msg, login_type=login_type)

    return render_template("login.html")


# ==================================================
# FIRST LAUNCH SETUP
# ==================================================

@app.route("/setup", methods=["GET", "POST"])
def setup():

    # If setup is already completed, don't allow setup again
    if is_setup_complete():

        if session.get("logged_in"):
            return redirect("/billing")

        return redirect("/")

    if request.method == "POST":

        # ==========================================
        # BUSINESS DETAILS
        # ==========================================

        store_name = request.form.get(
            "store_name",
            ""
        ).strip()

        address = request.form.get(
            "address",
            ""
        ).strip()

        city = request.form.get(
            "city",
            ""
        ).strip()

        state = request.form.get(
            "state",
            ""
        ).strip()

        postal_code = request.form.get(
            "postal_code",
            ""
        ).strip()

        phone = request.form.get(
            "phone",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        gstin = request.form.get(
            "gstin",
            ""
        ).strip()

        # ==========================================
        # OWNER ACCOUNT
        # ==========================================

        owner_name = request.form.get(
            "owner_name",
            ""
        ).strip()

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        # ==========================================
        # INVOICE SETTINGS
        # ==========================================

        invoice_prefix = request.form.get(
            "invoice_prefix",
            "INV"
        ).strip()

        invoice_start_number = request.form.get(
            "invoice_start_number",
            "1"
        ).strip()

        invoice_footer = request.form.get(
            "invoice_footer",
            ""
        ).strip()

        # ==========================================
        # VALIDATION
        # ==========================================

        if not store_name:

            return render_template(
                "setup.html",
                error="Business / Store name is required"
            )

        if not address:

            return render_template(
                "setup.html",
                error="Address is required"
            )

        if not city:

            return render_template(
                "setup.html",
                error="City is required"
            )

        if not state:

            return render_template(
                "setup.html",
                error="State is required"
            )

        if not postal_code:

            return render_template(
                "setup.html",
                error="PIN / Postal Code is required"
            )

        if not phone:

            return render_template(
                "setup.html",
                error="Phone number is required"
            )

        if not owner_name:

            return render_template(
                "setup.html",
                error="Owner name is required"
            )

        if not username:

            return render_template(
                "setup.html",
                error="Username is required"
            )

        if not password:

            return render_template(
                "setup.html",
                error="Password is required"
            )

        if len(password) < 6:

            return render_template(
                "setup.html",
                error="Password must be at least 6 characters"
            )

        if password != confirm_password:

            return render_template(
                "setup.html",
                error="Passwords do not match"
            )

        if not invoice_prefix:

            invoice_prefix = "INV"

        try:

            invoice_start_number = int(
                invoice_start_number
            )

        except (ValueError, TypeError):

            return render_template(
                "setup.html",
                error="Invoice starting number must be a valid number"
            )

        if invoice_start_number < 1:

            return render_template(
                "setup.html",
                error="Invoice starting number must be greater than zero"
            )

        # ==========================================
        # DATABASE
        # ==========================================

        conn = get_connection()

        try:

            # --------------------------------------
            # CHECK USERNAME
            # --------------------------------------

            existing_user = conn.execute("""
                SELECT id
                FROM users
                WHERE username = ?
            """, (username,)).fetchone()

            if existing_user:

                conn.close()

                return render_template(
                    "setup.html",
                    error="Username already exists. Please choose another username."
                )

            # --------------------------------------
            # CREATE OWNER ACCOUNT
            # --------------------------------------

            password_hash = generate_password_hash(
                password
            )

            conn.execute("""
                INSERT INTO users (
                    username,
                    password,
                    owner_name
                )
                VALUES (?, ?, ?)
            """, (
                username,
                password_hash,
                owner_name
            ))

            # --------------------------------------
            # CREATE / UPDATE BUSINESS
            # --------------------------------------

            existing_business = conn.execute("""
                SELECT id
                FROM business
                WHERE id = 1
            """).fetchone()

            if existing_business:

                conn.execute("""
                    UPDATE business

                    SET
                        store_name = ?,
                        address = ?,
                        city = ?,
                        state = ?,
                        postal_code = ?,
                        phone = ?,
                        email = ?,
                        gstin = ?,
                        invoice_prefix = ?,
                        invoice_start_number = ?,
                        invoice_footer = ?,
                        setup_completed = 1

                    WHERE id = 1
                """, (
                    store_name,
                    address,
                    city,
                    state,
                    postal_code,
                    phone,
                    email,
                    gstin,
                    invoice_prefix,
                    invoice_start_number,
                    invoice_footer
                ))

            else:

                conn.execute("""
                    INSERT INTO business (
                        id,
                        store_name,
                        address,
                        city,
                        state,
                        postal_code,
                        phone,
                        email,
                        gstin,
                        invoice_prefix,
                        invoice_start_number,
                        invoice_footer,
                        setup_completed
                    )

                    VALUES (
                        1,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1
                    )
                """, (
                    store_name,
                    address,
                    city,
                    state,
                    postal_code,
                    phone,
                    email,
                    gstin,
                    invoice_prefix,
                    invoice_start_number,
                    invoice_footer
                ))

            conn.commit()

        except Exception as error:

            conn.rollback()
            conn.close()

            print("SETUP ERROR:", error)

            return render_template(
                "setup.html",
                error="Setup failed. Please try again."
            )

        conn.close()

        # ==========================================
        # AUTOMATIC LOGIN
        # ==========================================

        conn = get_connection()

        user = conn.execute("""
            SELECT
                id,
                username,
                owner_name
            FROM users
            WHERE username = ?
        """, (username,)).fetchone()

        conn.close()

        session["logged_in"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["owner_name"] = user["owner_name"]
        session["role"] = "admin"
        session["last_active"] = int(time.time())

        return redirect("/billing")

    return render_template("setup.html")

# ==================================================
# CLOUD STORE PLAN & TRIAL INFO
# ==================================================

def get_store_license_info(store_id=None):
    sid = store_id or session.get("store_id")
    if not sid:
        return {"status": "trial", "message": "Cloud Billing", "days_left": 14, "is_active": True}

    sub = get_store_subscription(sid)
    if sub["plan_status"] == "active":
        msg = f"⭐ Pro Cloud Active ({sub['time_left_str']} left)"
    elif sub["plan_status"] == "trial":
        msg = f"⚡ Free Trial Active ({sub['time_left_str']} left)"
    else:
        msg = "⚠️ Subscription Expired. Please activate Pro."

    return {
        "status": sub["plan_status"],
        "message": msg,
        "is_active": sub["is_active"],
        "days_left": sub.get("days_left", 0),
        "minutes_left": sub.get("minutes_left", 0),
        "seconds_left": sub.get("seconds_left", 0),
        "time_left_str": sub.get("time_left_str", ""),
        "expires_at": sub.get("expires_at", ""),
        "store_code": sub.get("store_code", ""),
        "store_name": sub.get("store_name", "")
    }


# ==================================================
# DASHBOARD
# ==================================================

@app.route("/dashboard")
def dashboard():

    if not session.get("logged_in"):
        return redirect("/")

    if session.get("role") == "staff":
        return redirect("/billing")

    store_id = session.get("store_id")
    info = get_store_license_info(store_id)
    return render_template("dashboard.html", license_info=info)


# ==================================================
# DASHBOARD API
# ==================================================

@app.route("/api/dashboard")
def dashboard_api():

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    store_id = session.get("store_id")
    conn = get_connection()

    # --------------------------------------
    # TODAY'S SALES & BILLS (Robust local + UTC matching)
    # --------------------------------------
    now_local = datetime.now()
    now_utc = datetime.utcnow()
    today_local = now_local.strftime("%Y-%m-%d")
    today_utc = now_utc.strftime("%Y-%m-%d")
    today_dmy = now_local.strftime("%d-%m-%Y")
    today_dmy_slash = now_local.strftime("%d/%m/%Y")

    start_today = f"{today_local} 00:00:00"
    end_today = f"{today_local} 23:59:59"
    prefix_today = f"{today_local}%"

    today_sales = conn.execute("""
        SELECT COALESCE(SUM(total), 0) AS total
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
          AND ((created_at BETWEEN ? AND ?) OR created_at LIKE ?)
    """, (store_id, store_id, start_today, end_today, prefix_today)).fetchone()

    # --------------------------------------
    # TODAY'S BILLS
    # --------------------------------------

    today_bills = conn.execute("""
        SELECT COUNT(*) AS count
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
          AND ((created_at BETWEEN ? AND ?) OR created_at LIKE ?)
    """, (store_id, store_id, start_today, end_today, prefix_today)).fetchone()

    # --------------------------------------
    # TOTAL BILLS
    # --------------------------------------

    total_bills = conn.execute("""
        SELECT COUNT(*) AS count
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
    """, (store_id, store_id)).fetchone()

    # --------------------------------------
    # TOTAL PRODUCTS
    # --------------------------------------

    total_products = conn.execute("""
        SELECT COUNT(*) AS count
        FROM products
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
    """, (store_id, store_id)).fetchone()

    # --------------------------------------
    # BUSINESS
    # --------------------------------------

    business = conn.execute("""
        SELECT
            store_name,
            phone,
            email,
            gstin
        FROM business
        WHERE store_id = ?
    """, (store_id,)).fetchone()
    if not business:
        business = conn.execute("SELECT store_name, phone, email, gstin FROM business ORDER BY id ASC LIMIT 1").fetchone()

    # --------------------------------------
    # WIDGET DATA
    # --------------------------------------

    top_customers_rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(customer_name), ''), 'Walk-in') AS name,
            COALESCE(NULLIF(TRIM(customer_phone), ''), '') AS phone,
            ROUND(SUM(total), 2) AS total_spent,
            COUNT(*) AS bills_count
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        GROUP BY name, phone
        ORDER BY total_spent DESC
        LIMIT 5
    """, (store_id, store_id)).fetchall()

    pending_dues_rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(customer_name), ''), 'Walk-in') AS name,
            COALESCE(NULLIF(TRIM(customer_phone), ''), '') AS phone,
            ROUND(SUM(total), 2) AS total_due,
            COUNT(*) AS bills_count
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
          AND (payment_method IN ('Credit', 'Pending', 'Loan', 'Unpaid') OR payment_status = 'Unpaid')
        GROUP BY name, phone
        ORDER BY total_due DESC
        LIMIT 5
    """, (store_id, store_id)).fetchall()

    highest_bills_rows = conn.execute("""
        SELECT
            bill_number,
            COALESCE(NULLIF(TRIM(customer_name), ''), 'Walk-in') AS customer_name,
            ROUND(total, 2) AS total,
            DATE(created_at, 'localtime') AS bill_date
        FROM bills
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        ORDER BY total DESC
        LIMIT 5
    """, (store_id, store_id)).fetchall()

    low_stock_rows = conn.execute("""
        SELECT
            name,
            stock_quantity,
            price_basis
        FROM products
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
          AND stock_quantity <= 10
        ORDER BY stock_quantity ASC
        LIMIT 5
    """, (store_id, store_id)).fetchall()

    conn.close()

    res = jsonify({
        "today_sales": float(today_sales["total"] or 0),
        "today_bills": int(today_bills["count"] or 0),
        "total_bills": int(total_bills["count"] or 0),
        "total_products": int(total_products["count"] or 0),
        "app_version": f"v{APP_VERSION}",
        "business": dict(business) if business else {"store_name": "Billing Software", "phone": "", "email": "", "gstin": ""},
        "widgets": {
            "top_customers": [dict(r) for r in top_customers_rows],
            "pending_dues": [dict(r) for r in pending_dues_rows],
            "highest_bills": [dict(r) for r in highest_bills_rows],
            "low_stock": [dict(r) for r in low_stock_rows]
        }
    })
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    res.headers["Pragma"] = "no-cache"
    res.headers["Expires"] = "0"
    return res

# ==================================================
# STAFF & ATTENDANCE MANAGEMENT
# ==================================================

def _calc_worked_hours(c_in, c_out):
    if not c_in or not c_out:
        return 0.0
    try:
        from datetime import datetime
        for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
            try:
                t1 = datetime.strptime(c_in, fmt)
                break
            except Exception:
                t1 = None
        for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
            try:
                t2 = datetime.strptime(c_out, fmt)
                break
            except Exception:
                t2 = None
        if t1 and t2:
            diff = (t2 - t1).total_seconds() / 3600.0
            if diff < 0:
                diff += 24.0
            return round(diff, 2)
    except Exception:
        pass
    return 0.0

@app.route("/staff")
def staff_page():
    if not session.get("logged_in"):
        return redirect("/")
    if session.get("role") == "staff":
        return redirect("/billing")
    return render_template("staff.html")

@app.route("/api/staff", methods=["GET"])
def get_staff():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    store_id = session.get("store_id")
    conn = get_connection()
    rows = conn.execute("SELECT * FROM staff WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL)) ORDER BY name ASC", (store_id, store_id)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/staff", methods=["POST"])
def add_staff():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    address = (data.get("address") or "").strip()
    salary = float(data.get("salary") or 0)
    salary_type = data.get("salary_type") or "Monthly"
    joining_date = data.get("joining_date") or time.strftime("%Y-%m-%d")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    
    if not name:
        return jsonify({"error": "Staff name is required"}), 400

    # Strict 10-digit mobile number validation
    clean_phone = "".join([c for c in phone if c.isdigit()])
    if len(clean_phone) != 10:
        return jsonify({"error": "Mobile number must be strictly 10 digits"}), 400
    phone = clean_phone
        
    store_id = session.get("store_id")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO staff (store_id, name, phone, address, salary, salary_type, joining_date) VALUES (?, ?, ?, ?, ?, ?, ?)", (store_id, name, phone, address, salary, salary_type, joining_date))
    new_staff_id = cursor.lastrowid

    if username and password:
        existing = conn.execute("SELECT id FROM users WHERE username = ? AND store_id = ?", (username, store_id)).fetchone()
        if existing:
            conn.close()
            return jsonify({"error": "Username already exists in this store"}), 400
        pwd_hash = generate_password_hash(password)
        conn.execute("INSERT INTO users (store_id, username, password, owner_name, role, staff_id) VALUES (?, ?, ?, ?, 'staff', ?)", (store_id, username, pwd_hash, name, new_staff_id))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": new_staff_id, "staff_id": new_staff_id})

@app.route("/api/staff/<int:staff_id>", methods=["PUT"])
def update_staff(staff_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if session.get("role") == "staff":
        return jsonify({"error": "Admin only"}), 403

    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    address = (data.get("address") or "").strip()
    salary = float(data.get("salary") or 0)
    salary_type = data.get("salary_type") or "Monthly"
    joining_date = data.get("joining_date") or time.strftime("%Y-%m-%d")

    if not name:
        return jsonify({"error": "Staff name is required"}), 400

    clean_phone = "".join([c for c in phone if c.isdigit()])
    if len(clean_phone) != 10:
        return jsonify({"error": "Mobile number must be strictly 10 digits"}), 400
    phone = clean_phone

    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute("SELECT id FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id)).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff member not found"}), 404

    conn.execute("""
        UPDATE staff 
        SET name = ?, phone = ?, address = ?, salary = ?, salary_type = ?, joining_date = ?
        WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
    """, (name, phone, address, salary, salary_type, joining_date, staff_id, store_id, store_id))

    conn.execute("UPDATE users SET owner_name = ? WHERE staff_id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (name, staff_id, store_id, store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/staff/credentials", methods=["POST"])
def set_staff_credentials():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if session.get("role") == "staff":
        return jsonify({"error": "Admin only"}), 403

    data = request.get_json() or {}
    staff_id = data.get("staff_id")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id)).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff member not found"}), 404

    pwd_hash = generate_password_hash(password)
    existing_user = conn.execute("SELECT id FROM users WHERE username = ? AND store_id = ?", (username, store_id)).fetchone()
    if existing_user:
        conn.execute("UPDATE users SET password = ?, owner_name = ?, role = 'staff', staff_id = ? WHERE id = ?", (pwd_hash, staff["name"], staff_id, existing_user["id"]))
    else:
        conn.execute("INSERT INTO users (store_id, username, password, owner_name, role, staff_id) VALUES (?, ?, ?, ?, 'staff', ?)", (store_id, username, pwd_hash, staff["name"], staff_id))
    
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Login credentials set for {staff['name']}"})

@app.route("/api/my-attendance", methods=["GET"])
def get_my_attendance():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_connection()
    user_id = session.get("user_id")
    store_id = session.get("store_id")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    
    staff = None
    if user and user["staff_id"]:
        staff = conn.execute("SELECT * FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (user["staff_id"], store_id, store_id)).fetchone()
    elif user and user["owner_name"]:
        staff = conn.execute("SELECT * FROM staff WHERE name = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (user["owner_name"], store_id, store_id)).fetchone()

    if not staff:
        conn.close()
        return jsonify({"error": "No linked staff record found"}), 404

    from datetime import date, timedelta
    today_str = date.today().isoformat()
    yest_str = (date.today() - timedelta(days=1)).isoformat()

    rows = conn.execute("""
        SELECT * FROM staff_attendance 
        WHERE staff_id = ? AND date IN (?, ?)
        ORDER BY date DESC
    """, (staff["id"], today_str, yest_str)).fetchall()
    
    conn.close()

    att_dict = { r["date"]: dict(r) for r in rows }
    return jsonify({
        "success": True,
        "staff": dict(staff),
        "today_date": today_str,
        "yesterday_date": yest_str,
        "attendance": att_dict
    })

@app.route("/api/my-attendance", methods=["POST"])
def save_my_attendance():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    target_date = (data.get("date") or "").strip()
    status = data.get("status") or "Present"
    clock_in = (data.get("clock_in") or "").strip()
    clock_out = (data.get("clock_out") or "").strip()
    notes = (data.get("notes") or "").strip()

    from datetime import date, timedelta
    today_str = date.today().isoformat()
    yest_str = (date.today() - timedelta(days=1)).isoformat()

    if target_date not in (today_str, yest_str):
        return jsonify({"error": "Attendance can only be marked for Today or Yesterday!"}), 400

    conn = get_connection()
    user_id = session.get("user_id")
    store_id = session.get("store_id")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    
    staff = None
    if user and user["staff_id"]:
        staff = conn.execute("SELECT * FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (user["staff_id"], store_id, store_id)).fetchone()
    elif user and user["owner_name"]:
        staff = conn.execute("SELECT * FROM staff WHERE name = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (user["owner_name"], store_id, store_id)).fetchone()

    if not staff:
        conn.close()
        return jsonify({"error": "No linked staff record found"}), 404

    hours = _calc_worked_hours(clock_in, clock_out) if status == "Present" else 0.0

    existing = conn.execute("SELECT id FROM staff_attendance WHERE staff_id = ? AND date = ?", (staff["id"], target_date)).fetchone()
    if existing:
        conn.execute("""
            UPDATE staff_attendance 
            SET clock_in = ?, clock_out = ?, status = ?, hours_worked = ?, notes = ?
            WHERE id = ?
        """, (clock_in, clock_out, status, hours, notes, existing["id"]))
    else:
        conn.execute("""
            INSERT INTO staff_attendance (staff_id, date, clock_in, clock_out, status, hours_worked, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (staff["id"], target_date, clock_in, clock_out, status, hours, notes))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Attendance saved for {target_date}"})

@app.route("/api/staff/<int:staff_id>", methods=["DELETE"])
def delete_staff(staff_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if session.get("role") == "staff":
        return jsonify({"error": "Admin only"}), 403

    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute("SELECT id FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id)).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    conn.execute("DELETE FROM staff_attendance WHERE staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM staff_extra_pay WHERE staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM staff_allowances WHERE staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM users WHERE staff_id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/staff/<int:staff_id>/details", methods=["GET"])
def get_staff_details(staff_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
        
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    month = request.args.get("month", "").strip()
    period = request.args.get("period", "").strip()
    
    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id)).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404
        
    # Determine date condition
    if from_date and to_date:
        date_cond = "date BETWEEN ? AND ?"
        date_params = (from_date, to_date)
    elif month:
        date_cond = "strftime('%Y-%m', date) = ?"
        date_params = (month,)
    elif period == "all":
        date_cond = "1=1"
        date_params = ()
    else:
        # Default to current month
        cur_month = time.strftime("%Y-%m")
        date_cond = "strftime('%Y-%m', date) = ?"
        date_params = (cur_month,)
        
    recent_att = conn.execute(f"""
        SELECT id, date, status, clock_in, clock_out, hours_worked, notes
        FROM staff_attendance
        WHERE staff_id = ? AND {date_cond}
        ORDER BY date DESC
    """, (staff_id, *date_params)).fetchall()
    
    extra_pays = conn.execute(f"""
        SELECT id, date, pay_type, amount, notes
        FROM staff_extra_pay
        WHERE staff_id = ? AND {date_cond}
        ORDER BY date DESC
    """, (staff_id, *date_params)).fetchall()
    
    allowances = conn.execute(f"""
        SELECT id, date, description, amount
        FROM staff_allowances
        WHERE staff_id = ? AND {date_cond}
        ORDER BY date DESC
    """, (staff_id, *date_params)).fetchall()
    
    summary = conn.execute(f"""
        SELECT 
            SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) as present_days,
            SUM(CASE WHEN status = 'Absent' THEN 1 ELSE 0 END) as absent_days,
            SUM(CASE WHEN status = 'Half Day' THEN 1 ELSE 0 END) as half_days,
            COALESCE(SUM(hours_worked), 0) as total_hours,
            COUNT(*) as total_days
        FROM staff_attendance
        WHERE staff_id = ? AND {date_cond}
    """, (staff_id, *date_params)).fetchone()
    
    tot_extra = sum(float(r["amount"] or 0) for r in extra_pays)
    tot_allow = sum(float(r["amount"] or 0) for r in allowances)
    
    sum_dict = dict(summary) if summary else {"present_days": 0, "absent_days": 0, "half_days": 0, "total_hours": 0, "total_days": 0}
    sum_dict["total_extra_pay"] = round(tot_extra, 2)
    sum_dict["total_allowances"] = round(tot_allow, 2)
    
    conn.close()
    return jsonify({
        "staff": dict(staff),
        "summary": sum_dict,
        "recent_attendance": [dict(r) for r in recent_att],
        "extra_pay": [dict(r) for r in extra_pays],
        "allowances": [dict(r) for r in allowances]
    })

# --------------------------------------------------
# ALLOWANCES & INVOICE ZIP DOWNLOAD
# --------------------------------------------------

@app.route("/api/staff/allowances", methods=["POST"])
def add_allowance():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    staff_id = data.get("staff_id")
    amount = float(data.get("amount") or 0)
    description = (data.get("description") or "Allowance").strip()
    date = data.get("date") or time.strftime("%Y-%m-%d")
    
    if not staff_id or amount <= 0:
        return jsonify({"error": "Valid staff and amount required"}), 400
        
    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute("SELECT id FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (staff_id, store_id, store_id)).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    conn.execute("INSERT INTO staff_allowances (staff_id, date, amount, description) VALUES (?, ?, ?, ?)", (staff_id, date, amount, description))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/staff/allowances/<int:allowance_id>", methods=["DELETE"])
def delete_allowance(allowance_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    store_id = session.get("store_id")
    conn = get_connection()
    conn.execute("""
        DELETE FROM staff_allowances 
        WHERE id = ? AND staff_id IN (
            SELECT id FROM staff WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        )
    """, (allowance_id, store_id, store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/reports/download-invoices-zip")
def download_invoices_zip():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
        
    start_date = request.args.get("start_date") or time.strftime("%Y-%m-01")
    end_date = request.args.get("end_date") or time.strftime("%Y-%m-%d")
    store_id = session.get("store_id")
    
    conn = get_connection()
    bills = conn.execute("""
        SELECT * FROM bills 
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
          AND DATE(created_at) BETWEEN DATE(?) AND DATE(?)
        ORDER BY created_at DESC
    """, (store_id, store_id, start_date, end_date)).fetchall()
    
    import zipfile, io
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for b in bills:
            items = conn.execute("SELECT * FROM bill_items WHERE bill_id = ?", (b["id"],)).fetchall()
            
            # Format text invoice for each bill
            lines = [
                f"==========================================",
                f"INVOICE #: {b['bill_number']}",
                f"Date: {b['created_at']}",
                f"Customer Name: {b['customer_name'] or 'Walk-in'}",
                f"Customer Phone: {b['customer_phone'] or '-'}",
                f"Payment Method: {b['payment_method']}",
                f"==========================================",
                f"ITEMS:",
            ]
            for item in items:
                lines.append(f" - {item['product_name']} x{item['quantity']} @ ₹{item['price']} = ₹{item['total']}")
            lines.extend([
                f"==========================================",
                f"TOTAL AMOUNT: ₹{b['total']:.2f}",
                f"=========================================="
            ])
            
            filename = f"Invoice_{b['bill_number']}_{b['created_at'][:10]}.txt"
            zip_file.writestr(filename, "\n".join(lines))
            
    conn.close()
    
    zip_buffer.seek(0)
    from flask import send_file
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"Invoices_{start_date}_to_{end_date}.zip"
    )

@app.route("/api/staff/attendance", methods=["GET"])
def get_attendance():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    target_date = request.args.get("date") or time.strftime("%Y-%m-%d")
    store_id = session.get("store_id")
    conn = get_connection()
    rows = conn.execute("""
        SELECT 
            s.id as staff_id, s.name, s.phone, s.salary,
            a.id as attendance_id, a.date, a.clock_in, a.clock_out,
            a.hours_worked, a.notes,
            COALESCE(a.status, 'Present') as status
        FROM staff s
        LEFT JOIN staff_attendance a ON s.id = a.staff_id AND a.date = ?
        WHERE (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
        ORDER BY s.name ASC
    """, (target_date, store_id, store_id)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/staff/attendance", methods=["POST"])
def save_attendance():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    records = data.get("records") or []
    target_date = data.get("date") or time.strftime("%Y-%m-%d")
    store_id = session.get("store_id")
    
    conn = get_connection()
    for rec in records:
        staff_id = rec.get("staff_id")
        clock_in = (rec.get("clock_in") or "").strip()
        clock_out = (rec.get("clock_out") or "").strip()
        status = rec.get("status") or "Present"
        notes = (rec.get("notes") or "").strip()
        
        if not staff_id:
            continue

        # Strict security check: staff must belong to this store
        belongs = conn.execute(
            "SELECT id FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))",
            (staff_id, store_id, store_id)
        ).fetchone()
        if not belongs:
            continue
            
        hours = _calc_worked_hours(clock_in, clock_out) if status in ('Present', 'Half Day') else 0.0
        
        existing = conn.execute("SELECT id FROM staff_attendance WHERE staff_id = ? AND date = ?", (staff_id, target_date)).fetchone()
        if existing:
            conn.execute("""
                UPDATE staff_attendance 
                SET clock_in = ?, clock_out = ?, status = ?, hours_worked = ?, notes = ?
                WHERE id = ?
            """, (clock_in, clock_out, status, hours, notes, existing["id"]))
        else:
            conn.execute("""
                INSERT INTO staff_attendance (staff_id, date, clock_in, clock_out, status, hours_worked, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (staff_id, target_date, clock_in, clock_out, status, hours, notes))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --------------------------------------------------
# EXTRA PAY / EXPENSES & MONTHLY REPORT
# --------------------------------------------------

@app.route("/api/staff/extra-pay", methods=["GET"])
def get_extra_pay():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    staff_id = request.args.get("staff_id")
    store_id = session.get("store_id")
    conn = get_connection()
    if staff_id:
        rows = conn.execute("""
            SELECT p.*, s.name as staff_name 
            FROM staff_extra_pay p 
            JOIN staff s ON p.staff_id = s.id 
            WHERE p.staff_id = ? AND (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
            ORDER BY p.date DESC
        """, (staff_id, store_id, store_id)).fetchall()
    else:
        rows = conn.execute("""
            SELECT p.*, s.name as staff_name 
            FROM staff_extra_pay p 
            JOIN staff s ON p.staff_id = s.id 
            WHERE (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
            ORDER BY p.date DESC LIMIT 50
        """, (store_id, store_id)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/staff/extra-pay", methods=["POST"])
def add_extra_pay():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    staff_id = data.get("staff_id")
    amount = float(data.get("amount") or 0)
    pay_type = data.get("pay_type") or "Food/Drinks"
    notes = (data.get("notes") or "").strip()
    pay_date = data.get("date") or time.strftime("%Y-%m-%d")
    
    if not staff_id or amount <= 0:
        return jsonify({"error": "Valid staff and amount required"}), 400
        
    store_id = session.get("store_id")
    conn = get_connection()
    staff = conn.execute(
        "SELECT id FROM staff WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))",
        (staff_id, store_id, store_id)
    ).fetchone()
    if not staff:
        conn.close()
        return jsonify({"error": "Staff not found in this store"}), 404

    conn.execute("""
        INSERT INTO staff_extra_pay (staff_id, date, amount, pay_type, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (staff_id, pay_date, amount, pay_type, notes))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/staff/extra-pay/<int:pay_id>", methods=["DELETE"])
def delete_extra_pay(pay_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    store_id = session.get("store_id")
    conn = get_connection()
    conn.execute("""
        DELETE FROM staff_extra_pay 
        WHERE id = ? AND staff_id IN (
            SELECT id FROM staff WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        )
    """, (pay_id, store_id, store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/staff/overview", methods=["GET"])
def get_staff_overview():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    store_id = session.get("store_id")
    conn = get_connection()
    
    from datetime import datetime, timedelta
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    today_present = conn.execute("""
        SELECT COUNT(*) as cnt 
        FROM staff_attendance a 
        JOIN staff s ON s.id = a.staff_id 
        WHERE a.date = ? AND a.status = 'Present' 
          AND (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
    """, (today_str, store_id, store_id)).fetchone()["cnt"]

    today_absent = conn.execute("""
        SELECT COUNT(*) as cnt 
        FROM staff_attendance a 
        JOIN staff s ON s.id = a.staff_id 
        WHERE a.date = ? AND a.status = 'Absent' 
          AND (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
    """, (today_str, store_id, store_id)).fetchone()["cnt"]
    
    yest_present = conn.execute("""
        SELECT COUNT(*) as cnt 
        FROM staff_attendance a 
        JOIN staff s ON s.id = a.staff_id 
        WHERE a.date = ? AND a.status = 'Present' 
          AND (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
    """, (yesterday_str, store_id, store_id)).fetchone()["cnt"]

    yest_absent = conn.execute("""
        SELECT COUNT(*) as cnt 
        FROM staff_attendance a 
        JOIN staff s ON s.id = a.staff_id 
        WHERE a.date = ? AND a.status = 'Absent' 
          AND (s.store_id = ? OR (? IS NULL AND s.store_id IS NULL))
    """, (yesterday_str, store_id, store_id)).fetchone()["cnt"]
    
    total_staff = conn.execute("""
        SELECT COUNT(*) as cnt 
        FROM staff 
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
    """, (store_id, store_id)).fetchone()["cnt"]
    
    conn.close()
    return jsonify({
        "total_staff": total_staff,
        "today": {"present": today_present, "absent": today_absent},
        "yesterday": {"present": yest_present, "absent": yest_absent}
    })

@app.route("/api/staff/monthly-report", methods=["GET"])
def get_monthly_staff_report():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    month = request.args.get("month") or time.strftime("%Y-%m") # YYYY-MM
    store_id = session.get("store_id")
    
    conn = get_connection()
    staff_members = conn.execute("""
        SELECT * FROM staff 
        WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        ORDER BY name ASC
    """, (store_id, store_id)).fetchall()
    
    report = []
    for s in staff_members:
        att = conn.execute("""
            SELECT 
                SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) as days_present,
                SUM(CASE WHEN status = 'Absent' THEN 1 ELSE 0 END) as days_absent,
                SUM(CASE WHEN status = 'Half Day' THEN 1 ELSE 0 END) as days_halfday,
                COALESCE(SUM(hours_worked), 0) as total_hours
            FROM staff_attendance 
            WHERE staff_id = ? AND strftime('%Y-%m', date) = ?
        """, (s["id"], month)).fetchone()
        
        extra = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total_extra
            FROM staff_extra_pay
            WHERE staff_id = ? AND strftime('%Y-%m', date) = ?
        """, (s["id"], month)).fetchone()
        
        report.append({
            "staff_id": s["id"],
            "name": s["name"],
            "phone": s["phone"],
            "salary": s["salary"],
            "days_present": att["days_present"] or 0,
            "days_absent": att["days_absent"] or 0,
            "days_halfday": att["days_halfday"] or 0,
            "total_hours": round(att["total_hours"] or 0, 1),
            "extra_pay": round(extra["total_extra"] or 0, 2)
        })
        
    conn.close()
    return jsonify({"month": month, "report": report})


# BILLING PAGE
# ==================================================

@app.route("/billing")
def billing():

    if not session.get("logged_in"):
        return redirect("/")

    settings = get_settings()

    return render_template(
        "billing.html",
        payment_methods=settings["payment_methods_list"],
        require_customer_name=bool(settings["require_customer_name"]),
        gst_enabled=bool(settings["gst_enabled"]),
        gst_rate=float(settings["gst_rate"] or 0)
    )


# ==================================================
# PRODUCT PAGE
# ==================================================

@app.route("/products")
def products():

    if not session.get("logged_in"):
        return redirect("/")

    return render_template("products.html")


# ==================================================
# GET PRODUCTS
# ==================================================

@app.route("/api/products", methods=["GET"])
def get_products():

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    store_id = session.get("store_id")
    conn = get_connection()

    products = conn.execute("""
        SELECT
            p.id,
            p.name,
            p.billing_name,
            p.price_basis,
            COALESCE(
                (SELECT pb.selling_price
                 FROM product_batches pb
                 WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                 ORDER BY pb.purchase_date ASC, pb.id ASC
                 LIMIT 1),
                p.price
            ) AS price,
            COALESCE(
                (SELECT pb.invested_price
                 FROM product_batches pb
                 WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                 ORDER BY pb.purchase_date ASC, pb.id ASC
                 LIMIT 1),
                p.purchase_price,
                0
            ) AS purchase_price,
            COALESCE(
                (SELECT pb.discount_percent
                 FROM product_batches pb
                 WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                 ORDER BY pb.purchase_date ASC, pb.id ASC
                 LIMIT 1),
                p.discount_percent,
                0
            ) AS discount_percent,
            COALESCE(p.barcode, '') AS barcode,
            p.stock_quantity

        FROM products p
        WHERE (p.store_id = ? OR (? IS NULL AND p.store_id IS NULL))
        ORDER BY p.name
    """, (store_id, store_id)).fetchall()

    # Fetch active batches for each product to support split batch billing
    active_batches = conn.execute("""
        SELECT id, product_id, purchase_date, quantity_remaining, selling_price,
               COALESCE(discount_percent, 0) AS discount_percent
        FROM product_batches
        WHERE quantity_remaining > 0 AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        ORDER BY purchase_date ASC, id ASC
    """, (store_id, store_id)).fetchall()

    conn.close()

    batches_by_prod = {}
    for b in active_batches:
        pid = b["product_id"]
        if pid not in batches_by_prod:
            batches_by_prod[pid] = []
        raw_sp = float(b["selling_price"] or 0)
        dp = float(b["discount_percent"] or 0)
        eff_sp = round(raw_sp * (1.0 - dp / 100.0), 2) if dp > 0 else raw_sp
        batches_by_prod[pid].append({
            "id": b["id"],
            "purchase_date": b["purchase_date"],
            "quantity_remaining": float(b["quantity_remaining"] or 0),
            "selling_price": raw_sp,
            "discount_percent": dp,
            "effective_price": eff_sp
        })

    is_admin = session.get("role") != "staff"

    return jsonify([
        {
            "id": product["id"],
            "name": product["name"],
            "billing_name": product["billing_name"],
            "price_basis": product["price_basis"],
            "price": product["price"],
            "purchase_price": product["purchase_price"] if is_admin else 0,
            "barcode": product["barcode"] or "",
            "discount_percent": float(product["discount_percent"] or 0),
            "stock_quantity": product["stock_quantity"],
            "batches": batches_by_prod.get(product["id"], [])
        }
        for product in products
    ])


# ==================================================
# ADD PRODUCT
# ==================================================

@app.route("/api/products", methods=["POST"])
def add_product():

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json() or {}

    name = data.get(
        "name",
        ""
    ).strip()

    billing_name = data.get(
        "billing_name",
        ""
    ).strip()

    price_basis = data.get(
        "price_basis",
        ""
    ).strip()

    price = data.get("price")
    purchase_price = data.get("purchase_price", 0)
    barcode = str(data.get("barcode", "")).strip()
    discount_percent = max(0.0, float(data.get("discount_percent") or 0.0))
    purchase_date = str(data.get("purchase_date", "")).strip() or date.today().isoformat()
    supplier_note = str(data.get("supplier_note", "")).strip()

    stock_quantity = data.get(
        "stock_quantity"
    )

    # --------------------------------------
    # VALIDATION
    # --------------------------------------

    if not name:

        return jsonify({
            "error": "Product name is required"
        }), 400

    if not billing_name:

        return jsonify({
            "error": "Billing name is required"
        }), 400

    if not price_basis:

        return jsonify({
            "error": "Price basis is required"
        }), 400

    try:

        price = float(price)
        purchase_price = float(purchase_price or 0)
        discount_percent = min(100.0, max(0.0, float(discount_percent or 0)))

        stock_quantity = float(
            stock_quantity
        )

    except (ValueError, TypeError):

        return jsonify({
            "error": "Invalid price or stock quantity"
        }), 400

    if price < 0 or purchase_price < 0:

        return jsonify({
            "error": "Price cannot be negative"
        }), 400

    if stock_quantity < 0:

        return jsonify({
            "error": "Stock quantity cannot be negative"
        }), 400

    # --------------------------------------
    # INSERT PRODUCT & INITIAL BATCH
    # --------------------------------------

    conn = get_connection()

    try:
        store_id = session.get("store_id")
        conn.execute("BEGIN")

        cursor = conn.execute("""
            INSERT INTO products (
                store_id,
                name,
                billing_name,
                price_basis,
                price,
                purchase_price,
                barcode,
                discount_percent,
                stock_quantity
            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            store_id,
            name,
            billing_name,
            price_basis,
            price,
            purchase_price,
            barcode,
            discount_percent,
            stock_quantity
        ))

        new_product_id = cursor.lastrowid

        if stock_quantity > 0:
            conn.execute("""
                INSERT INTO product_batches (
                    store_id, product_id, purchase_date, quantity_added, quantity_remaining,
                    invested_price, selling_price, discount_percent, supplier_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (store_id, new_product_id, purchase_date, stock_quantity, stock_quantity, purchase_price, price, discount_percent, supplier_note or "Initial stock"))

            conn.execute("""
                INSERT INTO stock_movements (
                    store_id, product_id, movement_type, quantity, stock_before, stock_after, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (store_id, new_product_id, "IN", stock_quantity, 0, stock_quantity, supplier_note or "Initial stock"))

        conn.commit()

    except Exception:

        conn.rollback()
        conn.close()

        return jsonify({
            "error": "Product name already exists"
        }), 400

    conn.close()

    return jsonify({
        "message": "Product added successfully",
        "product_id": new_product_id,
        "id": new_product_id
    }), 201


# ==================================================
# UPDATE PRODUCT
# ==================================================

@app.route(
    "/api/products/<int:product_id>",
    methods=["PUT"]
)
def update_product(product_id):

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json() or {}

    name = data.get(
        "name",
        ""
    ).strip()

    billing_name = data.get(
        "billing_name",
        ""
    ).strip()

    price_basis = data.get(
        "price_basis",
        ""
    ).strip()

    price = data.get("price")
    purchase_price = data.get("purchase_price", 0)
    barcode = str(data.get("barcode", "")).strip()
    discount_percent = max(0.0, float(data.get("discount_percent") or 0.0))

    stock_quantity = data.get(
        "stock_quantity"
    )

    # --------------------------------------
    # VALIDATION
    # --------------------------------------

    if not name:

        return jsonify({
            "error": "Product name is required"
        }), 400

    if not billing_name:

        return jsonify({
            "error": "Billing name is required"
        }), 400

    if not price_basis:

        return jsonify({
            "error": "Price basis is required"
        }), 400

    try:

        price = float(price)
        purchase_price = float(purchase_price or 0)
        discount_percent = min(100.0, max(0.0, float(discount_percent or 0)))

        stock_quantity = float(
            stock_quantity
        )

    except (ValueError, TypeError):

        return jsonify({
            "error": "Invalid price or stock quantity"
        }), 400

    if price < 0 or purchase_price < 0:

        return jsonify({
            "error": "Price cannot be negative"
        }), 400

    if stock_quantity < 0:

        return jsonify({
            "error": "Stock quantity cannot be negative"
        }), 400

    # --------------------------------------
    # UPDATE PRODUCT
    # --------------------------------------

    store_id = session.get("store_id")
    conn = get_connection()

    try:

        if session.get("role") == "staff":
            existing = conn.execute("SELECT purchase_price FROM products WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (product_id, store_id, store_id)).fetchone()
            if existing:
                purchase_price = float(existing["purchase_price"] or 0)

        cursor = conn.execute("""
            UPDATE products

            SET
                name = ?,
                billing_name = ?,
                price_basis = ?,
                price = ?,
                purchase_price = ?,
                barcode = ?,
                discount_percent = ?,
                stock_quantity = ?

            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (
            name,
            billing_name,
            price_basis,
            price,
            purchase_price,
            barcode,
            discount_percent,
            stock_quantity,
            product_id,
            store_id,
            store_id
        ))

        if cursor.rowcount == 0:

            conn.rollback()
            conn.close()

            return jsonify({
                "error": "Product not found"
            }), 404

        # Keep active batch rates synced
        active_b = conn.execute("""
            SELECT id FROM product_batches
            WHERE product_id = ? AND quantity_remaining > 0 AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
            ORDER BY purchase_date ASC, id ASC
            LIMIT 1
        """, (product_id, store_id, store_id)).fetchone()
        if active_b:
            conn.execute("""
                UPDATE product_batches
                SET selling_price = ?,
                    invested_price = ?
                WHERE id = ?
            """, (price, purchase_price, active_b["id"]))

        conn.commit()

    except Exception:

        conn.rollback()
        conn.close()

        return jsonify({
            "error": "Product name already exists"
        }), 400

    conn.close()

    return jsonify({
        "message": "Product updated successfully"
    })


# ==================================================
# PRODUCT REFILL & BATCHES
# ==================================================

@app.route("/api/products/<int:product_id>/refill", methods=["POST"])
def refill_product_stock(product_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    purchase_date = str(data.get("purchase_date", "")).strip() or date.today().isoformat()
    supplier_note = str(data.get("supplier_note", "")).strip()

    try:
        qty_added = float(data.get("quantity_added") or data.get("quantity") or 0)
        raw_invested = data.get("invested_price")
        raw_selling = data.get("selling_price")
        invested_price = float(raw_invested) if raw_invested is not None and str(raw_invested).strip() != "" else None
        selling_price = float(raw_selling) if raw_selling is not None and str(raw_selling).strip() != "" else None
        discount_percent = max(0.0, min(100.0, float(data.get("discount_percent") or 0.0)))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid quantity or price"}), 400

    if qty_added <= 0:
        return jsonify({"error": "Quantity added must be greater than 0"}), 400

    store_id = session.get("store_id")
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        prod = conn.execute("SELECT id, name, price, purchase_price, stock_quantity, COALESCE(discount_percent, 0) AS discount_percent FROM products WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (product_id, store_id, store_id)).fetchone()
        if not prod:
            conn.rollback()
            return jsonify({"error": "Product not found"}), 404

        if invested_price is None or invested_price < 0:
            invested_price = float(prod["purchase_price"] or 0)
        if selling_price is None or selling_price <= 0:
            selling_price = float(prod["price"] or 0)

        stock_before = float(prod["stock_quantity"] or 0)
        stock_after = round(stock_before + qty_added, 3)

        update_selling = selling_price if selling_price > 0 else float(prod["price"] or 0)

        # 1. Insert into product_batches with batch-level discount_percent and store_id
        conn.execute("""
            INSERT INTO product_batches (
                store_id, product_id, purchase_date, quantity_added, quantity_remaining,
                invested_price, selling_price, supplier_note, discount_percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (store_id, product_id, purchase_date, qty_added, qty_added, invested_price, update_selling, supplier_note or f"Refill ({purchase_date})", discount_percent))

        # 2. Update product stock and keep rates synced with current active FIFO batch
        active_b = conn.execute("""
            SELECT selling_price, invested_price, COALESCE(discount_percent, 0) AS discount_percent
            FROM product_batches
            WHERE product_id = ? AND quantity_remaining > 0 AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
            ORDER BY purchase_date ASC, id ASC
            LIMIT 1
        """, (product_id, store_id, store_id)).fetchone()

        active_selling = float(active_b["selling_price"]) if active_b else update_selling
        active_invested = float(active_b["invested_price"]) if active_b else invested_price
        active_disc = float(active_b["discount_percent"]) if active_b else discount_percent

        conn.execute("""
            UPDATE products
            SET stock_quantity = ?,
                purchase_price = ?,
                price = ?,
                discount_percent = ?
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (stock_after, active_invested, active_selling, active_disc, product_id, store_id, store_id))

        # 3. Insert stock movement
        conn.execute("""
            INSERT INTO stock_movements (store_id, product_id, movement_type, quantity, stock_before, stock_after, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (store_id, product_id, "IN", qty_added, stock_before, stock_after, f"Stock refill on {purchase_date} ({supplier_note})".strip()))

        conn.commit()
        return jsonify({
            "success": True,
            "message": "Stock refilled successfully",
            "stock_quantity": stock_after,
            "purchase_price": invested_price,
            "price": update_selling,
            "discount_percent": active_disc
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/products/<int:product_id>/batches", methods=["GET"])
def get_product_batches(product_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    conn = get_connection()
    try:
        prod = conn.execute("""
            SELECT id, name, price, purchase_price, stock_quantity, price_basis,
                   COALESCE(discount_percent, 0) AS discount_percent
            FROM products WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (product_id, store_id, store_id)).fetchone()
        if not prod:
            return jsonify({"error": "Product not found"}), 404

        batches = conn.execute("""
            SELECT
                id,
                product_id,
                purchase_date,
                quantity_added,
                quantity_remaining,
                (quantity_added - quantity_remaining) AS units_sold,
                invested_price,
                selling_price,
                COALESCE(discount_percent, 0) AS discount_percent,
                supplier_note,
                created_at
            FROM product_batches
            WHERE product_id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
            ORDER BY purchase_date DESC, id DESC
        """, (product_id, store_id, store_id)).fetchall()

        is_admin = session.get("role") != "staff"
        prod_disc_pct = float(prod["discount_percent"] or 0)

        batch_list = []
        total_invested = 0.0
        total_profit = 0.0

        for b in batches:
            invested = float(b["invested_price"] or 0)
            raw_selling = float(b["selling_price"] or 0)
            batch_disc_pct = float(b["discount_percent"] or 0)

            # Selling price reflects ONLY this batch's discount
            effective_disc_pct = batch_disc_pct
            catalog_discounted = round(raw_selling * (1.0 - effective_disc_pct / 100.0), 2) if effective_disc_pct > 0 else raw_selling
            sold = max(0.0, float(b["units_sold"] or 0))

            nominal_profit = round(sold * (catalog_discounted - invested), 2)

            # Query exact deductions recorded for this specific batch
            ded_row = conn.execute("""
                SELECT
                    COALESCE(SUM(cart_discount_allocated), 0) AS total_cart_disc,
                    COALESCE(SUM(quantity * unit_selling_price), 0) AS actual_revenue,
                    COALESCE(SUM(quantity * unit_cost_price), 0) AS actual_cost,
                    COALESCE(COUNT(*), 0) AS ded_count
                FROM batch_deductions
                WHERE batch_id = ?
            """, (b["id"],)).fetchone()

            if ded_row and ded_row["ded_count"] > 0:
                b_cart_disc = round(float(ded_row["total_cart_disc"] or 0), 2)
                act_rev = float(ded_row["actual_revenue"] or 0)
                act_cost = float(ded_row["actual_cost"] or 0)
                # Revenue minus cost minus cart-level discount
                actual_profit = round(act_rev - act_cost - b_cart_disc, 2)
            else:
                b_cart_disc = 0.0
                actual_profit = nominal_profit

            actual_profit_per_unit = round(actual_profit / sold, 2) if sold > 0 else round(catalog_discounted - invested, 2)

            total_invested += float(b["quantity_added"] or 0) * invested
            total_profit += actual_profit

            batch_list.append({
                "id": b["id"],
                "purchase_date": b["purchase_date"],
                "quantity_added": round(float(b["quantity_added"] or 0), 3),
                "quantity_remaining": round(float(b["quantity_remaining"] or 0), 3),
                "units_sold": round(sold, 3),
                "invested_price": round(invested, 2) if is_admin else None,
                "raw_selling_price": round(raw_selling, 2),
                "selling_price": round(catalog_discounted, 2),
                "discount_percent": batch_disc_pct,
                "profit_per_unit": actual_profit_per_unit if is_admin else None,
                "profit_earned": actual_profit if is_admin else None,
                "supplier_note": b["supplier_note"] or "",
                "created_at": b["created_at"]
            })

        return jsonify({
            "product_id": prod["id"],
            "product_name": prod["name"],
            "price_basis": prod["price_basis"],
            "current_stock": round(float(prod["stock_quantity"] or 0), 3),
            "discount_percent": prod_disc_pct,
            "batches": batch_list,
            "total_invested": round(total_invested, 2) if is_admin else None,
            "total_profit_earned": round(total_profit, 2) if is_admin else None
        })
    finally:
        conn.close()


@app.route("/api/products/<int:product_id>/batches/<int:batch_id>", methods=["DELETE"])
def delete_product_batch(product_id, batch_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    conn = get_connection()
    try:
        conn.execute("BEGIN")

        # 1. Check batch existence
        batch = conn.execute("""
            SELECT id, product_id, quantity_remaining, quantity_added,
                   (quantity_added - quantity_remaining) AS units_sold
            FROM product_batches
            WHERE id = ? AND product_id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (batch_id, product_id, store_id, store_id)).fetchone()

        if not batch:
            conn.rollback()
            return jsonify({"error": "Batch not found"}), 404

        qty_rem = float(batch["quantity_remaining"] or 0)

        # 2. Deduct remaining stock from product
        prod = conn.execute("SELECT id, name, stock_quantity FROM products WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (product_id, store_id, store_id)).fetchone()
        new_stock = 0.0
        if prod:
            current_stock = float(prod["stock_quantity"] or 0)
            new_stock = max(0.0, round(current_stock - qty_rem, 3))
            conn.execute("UPDATE products SET stock_quantity = ? WHERE id = ?", (new_stock, product_id))

            if qty_rem > 0:
                conn.execute("""
                    INSERT INTO stock_movements (store_id, product_id, movement_type, quantity, stock_before, stock_after, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (store_id, product_id, "OUT", qty_rem, current_stock, new_stock, f"Batch #{batch_id} deleted"))

        # 3. Delete the batch and its deductions
        conn.execute("DELETE FROM batch_deductions WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM product_batches WHERE id = ?", (batch_id,))

        # 4. Re-sync products.price, purchase_price, discount_percent to the next active batch
        active_b = conn.execute("""
            SELECT selling_price, invested_price, COALESCE(discount_percent, 0) AS discount_percent
            FROM product_batches
            WHERE product_id = ? AND quantity_remaining > 0
            ORDER BY purchase_date ASC, id ASC
            LIMIT 1
        """, (product_id,)).fetchone()

        if active_b:
            conn.execute("""
                UPDATE products
                SET price = ?, purchase_price = ?, discount_percent = ?
                WHERE id = ?
            """, (float(active_b["selling_price"]), float(active_b["invested_price"]), float(active_b["discount_percent"]), product_id))

        conn.commit()
        return jsonify({
            "success": True,
            "message": f"Batch #{batch_id} deleted successfully",
            "stock_quantity": new_stock
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/products/barcode/<path:barcode>", methods=["GET"])
def get_product_by_barcode(barcode):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    code = str(barcode).strip()
    if not code:
        return jsonify({"found": False, "error": "Barcode is required"}), 400

    store_id = session.get("store_id")
    conn = get_connection()
    try:
        prod = conn.execute("""
            SELECT
                p.id,
                p.name,
                p.billing_name,
                p.price_basis,
                COALESCE(
                    (SELECT pb.selling_price
                     FROM product_batches pb
                     WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                     ORDER BY pb.purchase_date ASC, pb.id ASC
                     LIMIT 1),
                    p.price
                ) AS price,
                COALESCE(
                    (SELECT pb.invested_price
                     FROM product_batches pb
                     WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                     ORDER BY pb.purchase_date ASC, pb.id ASC
                     LIMIT 1),
                    p.purchase_price,
                    0
                ) AS purchase_price,
                p.stock_quantity,
                COALESCE(p.barcode, '') AS barcode,
                COALESCE(
                    (SELECT pb.discount_percent
                     FROM product_batches pb
                     WHERE pb.product_id = p.id AND pb.quantity_remaining > 0
                     ORDER BY pb.purchase_date ASC, pb.id ASC
                     LIMIT 1),
                    p.discount_percent,
                    0
                ) AS discount_percent
            FROM products p
            WHERE (p.barcode = ? OR p.name = ?) AND (p.store_id = ? OR (? IS NULL AND p.store_id IS NULL))
            LIMIT 1
        """, (code, code, store_id, store_id)).fetchone()

        if not prod:
            return jsonify({"found": False, "message": "Product not found"}), 404

        active_batches = conn.execute("""
            SELECT id, purchase_date, quantity_remaining, selling_price,
                   COALESCE(discount_percent, 0) AS discount_percent
            FROM product_batches
            WHERE product_id = ? AND quantity_remaining > 0
            ORDER BY purchase_date ASC, id ASC
        """, (prod["id"],)).fetchall()

        batch_list = []
        for b in active_batches:
            raw_sp = float(b["selling_price"] or 0)
            dp = float(b["discount_percent"] or 0)
            eff_sp = round(raw_sp * (1.0 - dp / 100.0), 2) if dp > 0 else raw_sp
            batch_list.append({
                "id": b["id"],
                "purchase_date": b["purchase_date"],
                "quantity_remaining": float(b["quantity_remaining"] or 0),
                "selling_price": raw_sp,
                "discount_percent": dp,
                "effective_price": eff_sp
            })

        is_admin = session.get("role") != "staff"
        return jsonify({
            "found": True,
            "product": {
                "id": prod["id"],
                "name": prod["name"],
                "billing_name": prod["billing_name"],
                "price_basis": prod["price_basis"],
                "price": float(prod["price"] or 0),
                "purchase_price": float(prod["purchase_price"] or 0) if is_admin else 0,
                "stock_quantity": float(prod["stock_quantity"] or 0),
                "barcode": prod["barcode"] or "",
                "discount_percent": float(prod["discount_percent"] or 0),
                "batches": batch_list
            }
        })
    finally:
        conn.close()


# ==================================================
# DELETE PRODUCT
# ==================================================

@app.route(
    "/api/products/<int:product_id>",
    methods=["DELETE"]
)
def delete_product(product_id):

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    store_id = session.get("store_id")
    conn = get_connection()

    cursor = conn.execute("""
        DELETE FROM products
        WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
    """, (
        product_id, store_id, store_id
    ))

    conn.commit()

    deleted = cursor.rowcount

    conn.close()

    if deleted == 0:

        return jsonify({
            "error": "Product not found"
        }), 404

    return jsonify({
        "message": "Product deleted successfully"
    })

    conn.commit()

    deleted = cursor.rowcount

    conn.close()

    if deleted == 0:

        return jsonify({
            "error": "Product not found"
        }), 404

    return jsonify({
        "message": "Product deleted successfully"
    })


# ==================================================
# ADD PRODUCT STOCK
# ==================================================

@app.route(
    "/api/products/<int:product_id>/stock",
    methods=["POST"]
)
def add_product_stock(product_id):

    if "logged_in" not in session:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json() or {}

    try:
        quantity = float(data.get("quantity", 0))
    except (TypeError, ValueError):

        return jsonify({
            "error": "Invalid quantity"
        }), 400

    reason = str(
        data.get("reason", "Stock Added")
    ).strip() or "Stock Added"

    if quantity <= 0:

        return jsonify({
            "error": "Stock quantity must be greater than 0"
        }), 400

    store_id = session.get("store_id")
    conn = get_connection()

    try:

        product = conn.execute("""
            SELECT
                id,
                name,
                billing_name,
                price_basis,
                stock_quantity

            FROM products

            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (
            product_id,
            store_id,
            store_id
        )).fetchone()

        if product is None:

            return jsonify({
                "error": "Product not found"
            }), 404

        stock_before = float(product["stock_quantity"] or 0)
        stock_after = round(stock_before + quantity, 3)

        conn.execute("""
            UPDATE products

            SET stock_quantity = ?

            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (
            stock_after,
            product_id,
            store_id,
            store_id
        ))

        conn.execute("""
            INSERT INTO stock_movements (
                store_id,
                product_id,
                movement_type,
                quantity,
                stock_before,
                stock_after,
                reason
            )

            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            store_id,
            product_id,
            "ADD",
            quantity,
            stock_before,
            stock_after,
            reason
        ))

        conn.commit()

        return jsonify({
            "success": True,
            "message": "Stock added successfully",
            "product_id": product["id"],
            "name": product["name"],
            "price_basis": product["price_basis"],
            "quantity_added": quantity,
            "stock_before": stock_before,
            "stock_after": stock_after
        })

    except Exception as error:

        conn.rollback()

        return jsonify({
            "error": str(error)
        }), 500

    finally:

        conn.close()


# ==================================================
# GENERATE BILL
# ==================================================

@app.route("/api/bills", methods=["POST"])
def generate_bill():

    if not session.get("logged_in"):

        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json() or {}

    items = data.get(
        "items",
        []
    )

    if not items:

        return jsonify({
            "error": "Bill has no items"
        }), 400

    settings = get_settings()

    customer_name = " ".join(
        str(data.get("customer_name", "")).split()
    )

    if not customer_name:

        if settings["require_customer_name"]:

            return jsonify({
                "error": "Customer name is required"
            }), 400

        customer_name = "Walk-in Customer"

    if len(customer_name) > 100:

        return jsonify({
            "error": "Customer name is too long (max 100 characters)"
        }), 400

    customer_phone = str(data.get("customer_phone") or "").strip()

    payment_method = str(
        data.get("payment_method") or "Cash"
    ).strip()

    valid_methods = settings["payment_methods_list"] + ["Pending", "Loan", "Unpaid", "Credit"]
    if payment_method not in valid_methods:
        return jsonify({
            "error": "Invalid payment method"
        }), 400

    payment_status = "Unpaid" if payment_method in ["Pending", "Loan", "Unpaid", "Credit"] else "Paid"

    conn = get_connection()

    try:

        # ==========================================
        # START TRANSACTION
        # ==========================================

        conn.execute("BEGIN")

        bill_items = []

        grand_total = 0.0

        # ==========================================
        # VALIDATE EVERY ITEM
        # ==========================================

        for item in items:

            try:
                prod_id_val = item.get("product_id")
                product_id = int(prod_id_val) if prod_id_val is not None else None

                base_quantity = float(item.get("quantity"))
                display_quantity = float(item.get("quantity_display", base_quantity))
                
                # For custom items, price and name must come from payload
                if product_id is None:
                    custom_price = float(item.get("price", 0))
                    custom_name = str(item.get("product_name", "Custom Item"))
                    custom_basis = str(item.get("price_basis", "Piece"))
            except (ValueError, TypeError):
                raise ValueError("Invalid product or quantity")

            quantity_unit = item.get("quantity_unit", "")

            if base_quantity <= 0:
                raise ValueError("Quantity must be greater than zero")

            # ======================================
            # GET PRODUCT OR USE CUSTOM
            # ======================================

            if product_id is not None:
                product = conn.execute("""
                    SELECT id, name, billing_name, price, price_basis, stock_quantity, COALESCE(discount_percent, 0) AS discount_percent
                    FROM products
                    WHERE id = ?
                """, (product_id,)).fetchone()

                if not product:
                    raise ValueError("Product not found")

                available_stock = float(product["stock_quantity"])

                if base_quantity > available_stock:
                    raise ValueError(
                        f'Insufficient stock for "{product["billing_name"]}". '
                        f'Available: {available_stock:g} {product["price_basis"]}'
                    )

                p_discount = float(product["discount_percent"] or 0)
                item_batch_id = int(item["batch_id"]) if item.get("batch_id") else None
                item_sent_price = float(item["price"]) if item.get("price") is not None and str(item.get("price")).strip() != "" else None
                item_disc = 0.0

                if item.get("discountPercent") is not None:
                    item_disc = max(0.0, float(item.get("discountPercent") or 0.0))
                elif item.get("discount_percent") is not None:
                    item_disc = max(0.0, float(item.get("discount_percent") or 0.0))

                if item_sent_price is not None and item_sent_price >= 0:
                    # Explicit price entered or edited in cart by cashier
                    price = round(item_sent_price, 2)
                    item_total = round(base_quantity * price, 2)
                elif item_batch_id:
                    # Dedicated batch specified
                    b_row = conn.execute("""
                        SELECT selling_price, COALESCE(discount_percent, 0) AS discount_percent
                        FROM product_batches
                        WHERE id = ? AND product_id = ?
                    """, (item_batch_id, product_id)).fetchone()
                    if b_row:
                        raw_b_sell = float(b_row["selling_price"] or 0)
                        b_disc = float(b_row["discount_percent"] or 0)
                        if item.get("discountPercent") is None and item.get("discount_percent") is None:
                            item_disc = b_disc
                        effective_disc = b_disc
                        price = round(raw_b_sell * (1.0 - effective_disc / 100.0), 2) if effective_disc > 0 else raw_b_sell
                        item_total = round(base_quantity * price, 2)
                    else:
                        price = float(product["price"])
                        item_total = round(base_quantity * price, 2)
                else:
                    # Dynamically calculate price based on FIFO active batches
                    active_batches = conn.execute("""
                        SELECT id, quantity_remaining, invested_price, selling_price,
                               COALESCE(discount_percent, 0) AS discount_percent
                        FROM product_batches
                        WHERE product_id = ? AND quantity_remaining > 0
                        ORDER BY purchase_date ASC, id ASC
                    """, (product_id,)).fetchall()

                    if active_batches:
                        if item.get("discountPercent") is None and item.get("discount_percent") is None:
                            item_disc = float(active_batches[0]["discount_percent"] or 0)
                        qty_to_calc = base_quantity
                        total_selling = 0.0
                        for ab in active_batches:
                            b_rem = float(ab["quantity_remaining"] or 0)
                            b_sell = float(ab["selling_price"] or 0)
                            ab_disc = float(ab["discount_percent"] or 0)
                            effective_disc = ab_disc
                            b_disc_sell = round(b_sell * (1.0 - effective_disc / 100.0), 2) if effective_disc > 0 else b_sell
                            take = min(qty_to_calc, b_rem)
                            if take > 0:
                                total_selling += take * b_disc_sell
                                qty_to_calc -= take
                            if qty_to_calc <= 0:
                                break
                        if qty_to_calc > 0:
                            raw_price = float(product["price"])
                            fb_disc = round(raw_price * (1.0 - p_discount / 100.0), 2) if p_discount > 0 else raw_price
                            total_selling += qty_to_calc * fb_disc

                        price = round(total_selling / base_quantity, 2)
                        item_total = round(total_selling, 2)
                    else:
                        raw_price = float(product["price"])
                        price = round(raw_price * (1.0 - p_discount / 100.0), 2) if p_discount > 0 else raw_price
                        item_total = round(base_quantity * price, 2)

                p_name = item.get("product_name") or product["billing_name"]
                p_basis = product["price_basis"]
            else:
                price = custom_price
                p_name = custom_name
                p_basis = custom_basis
                item_total = round(base_quantity * price, 2)
                item_batch_id = None
                item_disc = 0.0

            # ======================================
            # CALCULATE ITEM TOTAL
            # ======================================

            grand_total += item_total

            # ======================================
            # STORE BILL ITEM
            # ======================================

            bill_items.append({
                "product_id": product_id,
                "batch_id": item_batch_id,
                "product_name": p_name,
                "quantity": display_quantity,
                "quantity_unit": quantity_unit,
                "base_quantity": base_quantity,
                "price": price,
                "price_basis": p_basis,
                "total": item_total,
                "discount_percent": item_disc
            })

        # ==========================================
        # GET BUSINESS SETTINGS
        # ==========================================

        store_id = session.get("store_id")

        business = conn.execute("""
            SELECT
                invoice_prefix,
                invoice_start_number
            FROM business
            WHERE store_id = ?
        """, (store_id,)).fetchone()

        if not business:
            business = conn.execute("SELECT invoice_prefix, invoice_start_number FROM business ORDER BY id ASC LIMIT 1").fetchone()

        if business:

            invoice_prefix = (
                business["invoice_prefix"]
                or "INV"
            )

            invoice_start_number = int(
                business["invoice_start_number"]
                or 1
            )

        else:

            invoice_prefix = "INV"
            invoice_start_number = 1

        # ==========================================
        # GENERATE BILL NUMBER
        # ==========================================

        last_bill = conn.execute("""
            SELECT id
            FROM bills
            WHERE store_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (store_id,)).fetchone()

        if last_bill:

            next_number = max(
                int(last_bill["id"]) + invoice_start_number,
                invoice_start_number
            )

        else:

            next_number = invoice_start_number

        bill_number = (
            f"{invoice_prefix}-{next_number:06d}"
        )

        # ==========================================
        # DISCOUNT (Percentage % OR Direct Flat Rupee Less ₹)
        # ==========================================
        discount_percent = max(0.0, float(data.get("discount_percent") or 0.0))
        flat_discount = max(0.0, float(data.get("flat_discount") or 0.0))

        subtotal_before_discount = grand_total
        pct_discount_val = round(subtotal_before_discount * (discount_percent / 100.0), 2)
        total_discount_amount = min(subtotal_before_discount, round(pct_discount_val + flat_discount, 2))
        taxable_subtotal = max(0.0, round(subtotal_before_discount - total_discount_amount, 2))
        grand_total = taxable_subtotal

        # ==========================================
        # GST (added ON TOP of discounted product prices)
        # ==========================================

        tax_rate = 0.0
        tax_amount = 0.0

        apply_gst = bool(data.get("apply_gst", False))

        if settings["gst_enabled"] and apply_gst:

            tax_rate = float(settings["gst_rate"] or 0)

            # GST is calculated on discounted subtotal:
            tax_amount = round(
                grand_total * tax_rate / 100,
                2
            )

            # Grand total now includes GST
            grand_total = round(grand_total + tax_amount, 2)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor = conn.execute("""
            INSERT INTO bills (
                store_id,
                bill_number,
                customer_name,
                customer_phone,
                payment_method,
                payment_status,
                total,
                tax_rate,
                tax_amount,
                discount_percent,
                discount_amount,
                flat_discount,
                created_at
            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            store_id,
            bill_number,
            customer_name,
            customer_phone,
            payment_method,
            payment_status,
            grand_total,
            tax_rate,
            tax_amount,
            discount_percent,
            total_discount_amount,
            flat_discount,
            now_str
        ))

        bill_id = cursor.lastrowid

        # ==========================================
        # SAVE BILL ITEMS + REDUCE STOCK
        # ==========================================

        for item in bill_items:

            # --------------------------------------
            # DEDUCT FROM FIFO BATCHES & CALCULATE COST PRICE
            # --------------------------------------
            unit_cost_price = 0.0
            deductions_for_item = []

            if item["product_id"] is not None:
                qty_needed = float(item["base_quantity"])
                total_cost_calculated = 0.0
                target_batch_id = item.get("batch_id")

                # If a specific batch was designated for this item, deduct from it first
                if target_batch_id:
                    spec_b = conn.execute("""
                        SELECT id, quantity_remaining, invested_price
                        FROM product_batches
                        WHERE id = ? AND product_id = ?
                    """, (target_batch_id, item["product_id"])).fetchone()
                    if spec_b:
                        rem = float(spec_b["quantity_remaining"] or 0)
                        b_cost = float(spec_b["invested_price"] or 0)
                        take = min(qty_needed, rem)
                        if take > 0:
                            conn.execute("""
                                UPDATE product_batches
                                SET quantity_remaining = quantity_remaining - ?
                                WHERE id = ?
                            """, (take, spec_b["id"]))
                            total_cost_calculated += take * b_cost
                            qty_needed -= take
                            deductions_for_item.append({
                                "batch_id": spec_b["id"],
                                "take": take,
                                "cost": b_cost
                            })

                # If still needed (or no specific batch), deduct from active batches in FIFO order
                if qty_needed > 0:
                    batches = conn.execute("""
                        SELECT id, quantity_remaining, invested_price
                        FROM product_batches
                        WHERE product_id = ? AND quantity_remaining > 0
                        ORDER BY purchase_date ASC, id ASC
                    """, (item["product_id"],)).fetchall()

                    for b in batches:
                        rem = float(b["quantity_remaining"] or 0)
                        b_cost = float(b["invested_price"] or 0)
                        take = min(qty_needed, rem)
                        if take > 0:
                            conn.execute("""
                                UPDATE product_batches
                                SET quantity_remaining = quantity_remaining - ?
                                WHERE id = ?
                            """, (take, b["id"]))
                            total_cost_calculated += take * b_cost
                            qty_needed -= take
                            deductions_for_item.append({
                                "batch_id": b["id"],
                                "take": take,
                                "cost": b_cost
                            })
                        if qty_needed <= 0:
                            break

                # If any qty not covered by batches, fallback to product's purchase_price
                if qty_needed > 0:
                    prod_info = conn.execute(
                        "SELECT purchase_price FROM products WHERE id = ?",
                        (item["product_id"],)
                    ).fetchone()
                    default_cost = float(prod_info["purchase_price"] or 0) if prod_info else 0.0
                    total_cost_calculated += qty_needed * default_cost

                if float(item["base_quantity"]) > 0:
                    unit_cost_price = round(total_cost_calculated / float(item["base_quantity"]), 2)

            # --------------------------------------
            # SAVE BILL ITEM
            # --------------------------------------

            cursor_item = conn.execute("""
                INSERT INTO bill_items (
                    store_id,
                    bill_id,
                    product_id,
                    product_name,
                    quantity,
                    quantity_unit,
                    base_quantity,
                    price,
                    price_basis,
                    total,
                    cost_price

                )

                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            """, (
                store_id,
                bill_id,

                item["product_id"],

                item["product_name"],

                item["quantity"],

                item["quantity_unit"],

                item["base_quantity"],

                item["price"],

                item["price_basis"],

                item["total"],

                unit_cost_price

            ))

            bill_item_id = cursor_item.lastrowid

            # --------------------------------------
            # RECORD BATCH DEDUCTIONS & ALLOCATE CART DISCOUNT
            # --------------------------------------
            if item["product_id"] is not None and deductions_for_item:
                item_cart_disc = (item["total"] / subtotal_before_discount) * total_discount_amount if subtotal_before_discount > 0 else 0.0
                total_base_qty = float(item["base_quantity"])

                for d in deductions_for_item:
                    batch_cart_disc = item_cart_disc * (d["take"] / total_base_qty) if total_base_qty > 0 else 0.0
                    conn.execute("""
                        INSERT INTO batch_deductions (
                            store_id, bill_id, bill_item_id, product_id, batch_id,
                            quantity, unit_selling_price, unit_cost_price,
                            cart_discount_allocated, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        store_id,
                        bill_id,
                        bill_item_id,
                        item["product_id"],
                        d["batch_id"],
                        d["take"],
                        float(item["price"]),
                        d["cost"],
                        round(batch_cart_disc, 2),
                        now_str
                    ))

            # --------------------------------------
            # REDUCE STOCK & AUTO-ADVANCE PRODUCT PRICE TO NEXT ACTIVE BATCH
            # --------------------------------------

            if item["product_id"] is not None:
                # Find the next active batch after deduction
                next_batch = conn.execute("""
                    SELECT selling_price, invested_price, COALESCE(discount_percent, 0) AS discount_percent
                    FROM product_batches
                    WHERE product_id = ? AND quantity_remaining > 0
                    ORDER BY purchase_date ASC, id ASC
                    LIMIT 1
                """, (item["product_id"],)).fetchone()

                if next_batch:
                    conn.execute("""
                        UPDATE products
                        SET stock_quantity = stock_quantity - ?,
                            price = ?,
                            purchase_price = ?,
                            discount_percent = ?
                        WHERE id = ?
                    """, (
                        item["base_quantity"],
                        float(next_batch["selling_price"]),
                        float(next_batch["invested_price"]),
                        float(next_batch["discount_percent"]),
                        item["product_id"]
                    ))
                else:
                    conn.execute("""
                        UPDATE products
                        SET stock_quantity = stock_quantity - ?
                        WHERE id = ?
                    """, (
                        item["base_quantity"],
                        item["product_id"]
                    ))

        # ==========================================
        # COMMIT
        # ==========================================

        conn.commit()

        return jsonify({

            "success": True,

            "message":
                "Bill generated successfully",

            "bill_id":
                int(bill_id),

            "bill_number":
                bill_number,

            "customer_name":
                customer_name,

            "total":
                round(grand_total, 2)

        }), 201

    except Exception as error:

        # ==========================================
        # ROLLBACK
        # ==========================================

        conn.rollback()

        return jsonify({

            "error":
                str(error)

        }), 400

    finally:

        conn.close()


# ==================================================
# INVOICE PAGE
# ==================================================

@app.route("/invoice/<int:bill_id>")
def invoice_page(bill_id):

    if not session.get("logged_in"):
        return redirect("/")

    store_id = session.get("store_id")
    invoice = get_invoice_data(
        bill_id, store_id=store_id
    )

    if not invoice:

        return "Invoice not found", 404

    bill_store_id = invoice["bill"]["store_id"] if invoice["bill"] else store_id
    return render_template(
        "invoice.html",
        bill=invoice["bill"],
        items=invoice["items"],
        business=invoice["business"],
        settings=get_settings(store_id=bill_store_id)
    )
# ==================================================
# DELETE BILL (Restores stock for all items)
# ==================================================

@app.route("/api/bills/<int:bill_id>", methods=["DELETE"])
def delete_bill(bill_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        conn.execute("BEGIN")

        bill = conn.execute("""
            SELECT id, bill_number
            FROM bills
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (bill_id, store_id, store_id)).fetchone()

        if not bill:
            conn.rollback()
            return jsonify({"error": "Bill not found"}), 404

        # Fetch all items in this bill
        items = conn.execute("""
            SELECT
                id,
                product_id,
                product_name,
                base_quantity,
                price_basis
            FROM bill_items
            WHERE bill_id = ?
        """, (bill_id,)).fetchall()

        # Restore stock using batch_deductions if available
        bill_deductions = conn.execute("""
            SELECT id, product_id, batch_id, quantity
            FROM batch_deductions
            WHERE bill_id = ?
        """, (bill_id,)).fetchall()

        if bill_deductions:
            for d in bill_deductions:
                b_id = d["batch_id"]
                d_qty = float(d["quantity"] or 0)
                if b_id and d_qty > 0:
                    conn.execute("""
                        UPDATE product_batches
                        SET quantity_remaining = quantity_remaining + ?
                        WHERE id = ?
                    """, (d_qty, b_id))
            conn.execute("DELETE FROM batch_deductions WHERE bill_id = ?", (bill_id,))

        # Restore stock for each product
        for item in items:
            product_id = item["product_id"]
            qty = float(item["base_quantity"] or 0)

            if product_id is not None and qty > 0:
                prod = conn.execute("""
                    SELECT stock_quantity
                    FROM products
                    WHERE id = ?
                """, (product_id,)).fetchone()

                if prod:
                    stock_before = float(prod["stock_quantity"] or 0)
                    stock_after = round(stock_before + qty, 3)

                    conn.execute("""
                        UPDATE products
                        SET stock_quantity = ?
                        WHERE id = ?
                    """, (stock_after, product_id))

                    # If this bill had no batch_deductions (legacy bill), use fallback
                    if not bill_deductions:
                        restore_needed = qty
                        batches = conn.execute("""
                            SELECT id, quantity_added, quantity_remaining
                            FROM product_batches
                            WHERE product_id = ?
                            ORDER BY purchase_date DESC, id DESC
                        """, (product_id,)).fetchall()
                        for b in batches:
                            can_add = float(b["quantity_added"] or 0) - float(b["quantity_remaining"] or 0)
                            if can_add > 0:
                                take_add = min(restore_needed, can_add)
                                conn.execute("""
                                    UPDATE product_batches
                                    SET quantity_remaining = quantity_remaining + ?
                                    WHERE id = ?
                                """, (take_add, b["id"]))
                                restore_needed -= take_add
                            if restore_needed <= 0:
                                break

                    # Re-sync products.price, purchase_price, and discount_percent to active FIFO batch
                    active_b = conn.execute("""
                        SELECT selling_price, invested_price, COALESCE(discount_percent, 0) AS discount_percent
                        FROM product_batches
                        WHERE product_id = ? AND quantity_remaining > 0
                        ORDER BY purchase_date ASC, id ASC
                        LIMIT 1
                    """, (product_id,)).fetchone()
                    if active_b:
                        conn.execute("""
                            UPDATE products
                            SET price = ?, purchase_price = ?, discount_percent = ?
                            WHERE id = ?
                        """, (float(active_b["selling_price"]), float(active_b["invested_price"]), float(active_b["discount_percent"]), product_id))

                    conn.execute("""
                        INSERT INTO stock_movements (
                            product_id,
                            movement_type,
                            quantity,
                            stock_before,
                            stock_after,
                            reason
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        product_id,
                        "RETURN",
                        qty,
                        stock_before,
                        stock_after,
                        f"Bill {bill['bill_number']} deleted"
                    ))

        # Delete all items and the bill
        conn.execute("DELETE FROM bill_items WHERE bill_id = ?", (bill_id,))
        conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))

        conn.commit()

        return jsonify({
            "success": True,
            "message": f"Bill {bill['bill_number']} deleted successfully and stock restored.",
            "items_restored": len(items)
        })

    except Exception as error:
        conn.rollback()
        return jsonify({"error": str(error)}), 500

    finally:
        conn.close()


# ==================================================
# UPDATE BILL PAYMENT STATUS (For marking loan/unpaid bills as paid)
# ==================================================

@app.route("/api/bills/<int:bill_id>/payment-status", methods=["POST"])
def update_bill_payment_status(bill_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    payment_status = str(data.get("payment_status", "Paid")).strip()
    payment_method = str(data.get("payment_method", "Cash")).strip()

    valid_map = {
        "paid": "Paid",
        "unpaid": "Unpaid",
        "pending": "Pending",
        "loan": "Loan",
        "credit": "Credit"
    }
    normalized_status = valid_map.get(payment_status.lower(), payment_status.title())
    if normalized_status not in ["Paid", "Unpaid", "Pending", "Loan", "Credit"]:
        return jsonify({"error": "Invalid payment status"}), 400
    payment_status = normalized_status

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        bill = conn.execute("SELECT id, bill_number FROM bills WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))", (bill_id, store_id, store_id)).fetchone()
        if not bill:
            return jsonify({"error": "Bill not found"}), 404

        conn.execute("""
            UPDATE bills
            SET payment_status = ?, payment_method = ?
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (payment_status, payment_method, bill_id, store_id, store_id))
        conn.commit()

        return jsonify({
            "success": True,
            "bill_id": bill_id,
            "bill_number": bill["bill_number"],
            "payment_status": payment_status,
            "payment_method": payment_method
        })

    except Exception as error:
        conn.rollback()
        return jsonify({"error": str(error)}), 500

    finally:
        conn.close()


# ==================================================
# GET BILL ITEMS (For selecting and viewing bill items)
# ==================================================

@app.route("/api/bills/<int:bill_id>/items", methods=["GET"])
def get_bill_items(bill_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        bill = conn.execute("""
            SELECT
                id,
                bill_number,
                COALESCE(customer_name, '') AS customer_name,
                COALESCE(customer_phone, '') AS customer_phone,
                payment_method,
                COALESCE(payment_status, 'Paid') AS payment_status,
                total,
                COALESCE(discount_percent, 0) AS discount_percent,
                COALESCE(discount_amount, 0) AS discount_amount,
                COALESCE(flat_discount, 0) AS flat_discount,
                created_at
            FROM bills
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (bill_id, store_id, store_id)).fetchone()

        if not bill:
            return jsonify({"error": "Bill not found"}), 404

        items = conn.execute("""
            SELECT
                bi.id,
                bi.product_id,
                bi.product_name,
                bi.quantity,
                bi.quantity_unit,
                bi.base_quantity,
                bi.price,
                bi.price_basis,
                bi.total,
                COALESCE(p.discount_percent, 0) AS discount_percent
            FROM bill_items bi
            LEFT JOIN products p ON p.id = bi.product_id
            WHERE bi.bill_id = ?
            ORDER BY bi.id ASC
        """, (bill_id,)).fetchall()

        return jsonify({
            "success": True,
            "bill": dict(bill),
            "items": [dict(i) for i in items]
        })

    finally:
        conn.close()


# ==================================================
# DELETE BILL ITEM (Restores stock for that item)
# ==================================================

@app.route("/api/bills/<int:bill_id>/items/<int:item_id>", methods=["DELETE"])
def delete_bill_item(bill_id, item_id):

    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        conn.execute("BEGIN")

        bill = conn.execute("""
            SELECT id, bill_number, tax_rate
            FROM bills
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """, (bill_id, store_id, store_id)).fetchone()

        if not bill:
            conn.rollback()
            return jsonify({"error": "Bill not found"}), 404

        item = conn.execute("""
            SELECT
                id,
                product_id,
                product_name,
                base_quantity,
                price_basis
            FROM bill_items
            WHERE id = ? AND bill_id = ?
        """, (item_id, bill_id)).fetchone()

        if not item:
            conn.rollback()
            return jsonify({"error": "Item not found in this bill"}), 404

        product_id = item["product_id"]
        qty = float(item["base_quantity"] or 0)

        # Restore stock using batch_deductions if available
        item_deductions = conn.execute("""
            SELECT id, product_id, batch_id, quantity
            FROM batch_deductions
            WHERE bill_item_id = ?
        """, (item_id,)).fetchall()

        if item_deductions:
            for d in item_deductions:
                b_id = d["batch_id"]
                d_qty = float(d["quantity"] or 0)
                if b_id and d_qty > 0:
                    conn.execute("""
                        UPDATE product_batches
                        SET quantity_remaining = quantity_remaining + ?
                        WHERE id = ?
                    """, (d_qty, b_id))
            conn.execute("DELETE FROM batch_deductions WHERE bill_item_id = ?", (item_id,))

        # Restore stock if product exists
        if product_id is not None and qty > 0:
            prod = conn.execute("""
                SELECT stock_quantity
                FROM products
                WHERE id = ?
            """, (product_id,)).fetchone()

            if prod:
                stock_before = float(prod["stock_quantity"] or 0)
                stock_after = round(stock_before + qty, 3)

                conn.execute("""
                    UPDATE products
                    SET stock_quantity = ?
                    WHERE id = ?
                """, (stock_after, product_id))

                # If this item had no batch_deductions (legacy bill), use fallback
                if not item_deductions:
                    restore_needed = qty
                    batches = conn.execute("""
                        SELECT id, quantity_added, quantity_remaining
                        FROM product_batches
                        WHERE product_id = ?
                        ORDER BY purchase_date DESC, id DESC
                    """, (product_id,)).fetchall()
                    for b in batches:
                        can_add = float(b["quantity_added"] or 0) - float(b["quantity_remaining"] or 0)
                        if can_add > 0:
                            take_add = min(restore_needed, can_add)
                            conn.execute("""
                                UPDATE product_batches
                                SET quantity_remaining = quantity_remaining + ?
                                WHERE id = ?
                            """, (take_add, b["id"]))
                            restore_needed -= take_add
                        if restore_needed <= 0:
                            break

                # Re-sync products.price, purchase_price, and discount_percent to active FIFO batch
                active_b = conn.execute("""
                    SELECT selling_price, invested_price, COALESCE(discount_percent, 0) AS discount_percent
                    FROM product_batches
                    WHERE product_id = ? AND quantity_remaining > 0
                    ORDER BY purchase_date ASC, id ASC
                    LIMIT 1
                """, (product_id,)).fetchone()
                if active_b:
                    conn.execute("""
                        UPDATE products
                        SET price = ?, purchase_price = ?, discount_percent = ?
                        WHERE id = ?
                    """, (float(active_b["selling_price"]), float(active_b["invested_price"]), float(active_b["discount_percent"]), product_id))

                conn.execute("""
                    INSERT INTO stock_movements (
                        product_id,
                        movement_type,
                        quantity,
                        stock_before,
                        stock_after,
                        reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    product_id,
                    "RETURN",
                    qty,
                    stock_before,
                    stock_after,
                    f"Item '{item['product_name']}' removed from Bill {bill['bill_number']}"
                ))

        # Delete the item
        conn.execute("DELETE FROM bill_items WHERE id = ?", (item_id,))

        # Check remaining items
        remaining = conn.execute("""
            SELECT total
            FROM bill_items
            WHERE bill_id = ?
        """, (bill_id,)).fetchall()

        if not remaining:
            # All items deleted -> delete entire bill
            conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
            conn.commit()
            return jsonify({
                "success": True,
                "bill_deleted": True,
                "message": f"All items deleted. Bill {bill['bill_number']} was deleted and stock restored."
            })

        # Recalculate bill total and tax
        new_total = round(sum(float(r["total"] or 0) for r in remaining), 2)
        tax_rate = float(bill["tax_rate"] or 0)
        new_tax = round(new_total * tax_rate / (100 + tax_rate), 2) if tax_rate > 0 else 0.0

        conn.execute("""
            UPDATE bills
            SET total = ?, tax_amount = ?
            WHERE id = ?
        """, (new_total, new_tax, bill_id))

        conn.commit()

        return jsonify({
            "success": True,
            "bill_deleted": False,
            "new_total": new_total,
            "message": f"Item '{item['product_name']}' removed and stock restored."
        })

    except Exception as error:
        conn.rollback()
        return jsonify({"error": str(error)}), 500

    finally:
        conn.close()


# ============================================================
# BILL HISTORY PAGE
# ============================================================

@app.route("/bill-history")
def bill_history():

    if "logged_in" not in session:
        return redirect("/")

    return render_template("bill_history.html")


# ============================================================
# BILL HISTORY API
# ============================================================

@app.route("/api/bills/history", methods=["GET"])
def get_bill_history():

    if "logged_in" not in session:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    conn = get_connection()

    try:

        search = request.args.get(
            "search",
            ""
        ).strip()

        from_date = request.args.get(
            "from_date",
            ""
        ).strip()

        to_date = request.args.get(
            "to_date",
            ""
        ).strip()

        store_id = session.get("store_id")

        query = """
            SELECT
                b.id,
                b.bill_number,
                COALESCE(b.customer_name, '') AS customer_name,
                COALESCE(b.customer_phone, '') AS customer_phone,
                COALESCE(b.payment_method, '') AS payment_method,
                COALESCE(b.payment_status, 'Paid') AS payment_status,
                b.total,
                b.created_at,

                COUNT(bi.id) AS item_count

            FROM bills b

            LEFT JOIN bill_items bi
                ON b.id = bi.bill_id

            WHERE (b.store_id = ? OR (? IS NULL AND b.store_id IS NULL))
        """

        params = [store_id, store_id]

        # -------------------------
        # SEARCH
        # -------------------------

        if search:

            query += """
                AND (
                    b.bill_number LIKE ?
                    OR b.customer_name LIKE ?
                )
            """

            params.append(
                f"%{search}%"
            )

            params.append(
                f"%{search}%"
            )

        # -------------------------
        # FROM DATE
        # -------------------------

        if from_date:

            query += """
                AND DATE(b.created_at) >= DATE(?)
            """

            params.append(from_date)

        # -------------------------
        # TO DATE
        # -------------------------

        if to_date:

            query += """
                AND DATE(b.created_at) <= DATE(?)
            """

            params.append(to_date)

        # -------------------------
        # GROUP
        # -------------------------

        query += """
            GROUP BY
                b.id,
                b.bill_number,
                b.customer_name,
                b.customer_phone,
                b.payment_method,
                b.payment_status,
                b.total,
                b.created_at

            ORDER BY
                b.id DESC
        """

        rows = conn.execute(
            query,
            params
        ).fetchall()

        bills = [
            dict(row)
            for row in rows
        ]

        # -------------------------
        # SALES SUMMARY
        # -------------------------

        summary_query = """
            SELECT
                COUNT(*) AS total_bills,
                COALESCE(
                    SUM(total),
                    0
                ) AS total_sales

            FROM bills

            WHERE (store_id = ? OR (? IS NULL AND store_id IS NULL))
        """

        summary_params = [store_id, store_id]

        if search:

            summary_query += """
                AND (
                    bill_number LIKE ?
                    OR customer_name LIKE ?
                )
            """

            summary_params.append(
                f"%{search}%"
            )

            summary_params.append(
                f"%{search}%"
            )

        if from_date:

            summary_query += """
                AND DATE(created_at) >= DATE(?)
            """

            summary_params.append(
                from_date
            )

        if to_date:

            summary_query += """
                AND DATE(created_at) <= DATE(?)
            """

            summary_params.append(
                to_date
            )

        summary = conn.execute(
            summary_query,
            summary_params
        ).fetchone()

        return jsonify({
            "success": True,
            "bills": bills,
            "summary": {
                "total_bills": int(
                    summary["total_bills"] or 0
                ),
                "total_sales": float(
                    summary["total_sales"] or 0
                )
            }
        })

    except Exception as e:

        print(
            "BILL HISTORY ERROR:",
            e
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:

        conn.close()

# ==================================================
# ABOUT US
# ==================================================

@app.route("/about")
def about_page():
    return render_template("about.html")


# ==================================================
# PRO SUBSCRIPTION & LICENSE MANAGEMENT
# ==================================================

@app.route("/subscription")
def subscription_page():
    if not session.get("logged_in"):
        return redirect("/login")

    store_id = session.get("store_id")
    sub = get_store_subscription(store_id)
    return render_template(
        "subscription.html",
        plan_status=sub.get("plan_status", "trial"),
        is_active=sub.get("is_active", False),
        seconds_left=sub.get("seconds_left", 0),
        minutes_left=sub.get("minutes_left", 0),
        days_left=sub.get("days_left", 0),
        time_left_str=sub.get("time_left_str", "0m 0s"),
        expires_at=sub.get("expires_at", "Expired"),
        store_code=sub.get("store_code", session.get("store_code", "")),
        store_name=sub.get("store_name", session.get("store_name", "Your Store")),
        expired_flag=request.args.get("expired") == "1"
    )


@app.route("/license")
def license_redirect():
    return redirect("/subscription")


@app.route("/api/subscription/status")
def api_subscription_status():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    store_id = session.get("store_id")
    sub = get_store_subscription(store_id)
    return jsonify({"success": True, "subscription": sub})


@app.route("/api/subscription/pay-test", methods=["POST"])
def api_subscription_pay_test():
    """
    Simulates ₹5 test payment for 30 minutes of Pro access.
    Generates a unique license key, immediately redeems it for this store,
    and returns the unique key to the user.
    """
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    success, key_code, msg = process_test_payment(store_id, amount=5.0, duration_minutes=30)
    if success:
        sub = get_store_subscription(store_id)
        return jsonify({
            "success": True,
            "key_code": key_code,
            "message": msg,
            "expires_at": sub.get("expires_at"),
            "time_left_str": sub.get("time_left_str"),
            "seconds_left": sub.get("seconds_left")
        })
    return jsonify({"success": False, "error": msg or "Payment processing failed"}), 400


@app.route("/api/subscription/activate", methods=["POST"])
def api_subscription_activate():
    """
    Redeems a single-use unique license key for this store.
    Rejects keys already used by this or any other store.
    """
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    data = request.get_json(silent=True) or {}
    key = data.get("activation_key", "").strip()

    if not key:
        return jsonify({"success": False, "error": "Please enter a valid activation key."}), 400

    success, msg = redeem_license_key(key, store_id)
    if success:
        sub = get_store_subscription(store_id)
        return jsonify({
            "success": True,
            "message": msg,
            "expires_at": sub.get("expires_at"),
            "time_left_str": sub.get("time_left_str"),
            "seconds_left": sub.get("seconds_left")
        })
    return jsonify({"success": False, "error": msg}), 400


@app.route("/api/subscription/generate-key", methods=["POST"])
def api_subscription_generate_key():
    """
    Generates a new, unused unique license key.
    """
    if not session.get("logged_in") or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    duration = int(data.get("duration_minutes", 30))
    price = float(data.get("price", 5.0))
    plan_name = data.get("plan_name", f"{duration}-Minute Pro Access")

    key_code = generate_license_key(duration_minutes=duration, price=price, plan_name=plan_name)
    return jsonify({"success": True, "key_code": key_code, "duration_minutes": duration})


# ==================================================
# RAZORPAY PAYMENT GATEWAY INTEGRATION
# ==================================================

SUBSCRIPTION_PLANS = {
    "test_30m": {
        "name": "30-Minute Pro Access (Testing)",
        "amount_paise": 500,     # ₹5.00
        "price_rupees": 5.0,
        "duration_minutes": 30
    },
    "monthly": {
        "name": "Monthly Pro Plan (30 Days)",
        "amount_paise": 49900,   # ₹499.00
        "price_rupees": 499.0,
        "duration_minutes": 30 * 24 * 60  # 43200 minutes (30 days)
    },
    "quarterly": {
        "name": "3-Month Pro Plan (90 Days)",
        "amount_paise": 129900,  # ₹1,299.00
        "price_rupees": 1299.0,
        "duration_minutes": 90 * 24 * 60  # 129600 minutes (90 days)
    },
    "half_yearly": {
        "name": "6-Month Pro Plan (180 Days)",
        "amount_paise": 279900,  # ₹2,799.00
        "price_rupees": 2799.0,
        "duration_minutes": 180 * 24 * 60  # 259200 minutes (180 days)
    },
    "annual": {
        "name": "Annual Pro Plan (1 Year - Best Value)",
        "amount_paise": 399900,  # ₹3,999.00
        "price_rupees": 3999.0,
        "duration_minutes": 365 * 24 * 60 # 525600 minutes (365 days)
    }
}


def get_razorpay_client():
    """
    Returns (razorpay.Client, key_id) or (None, "") if not configured.
    Loads from environment variables or data/razorpay.json.
    """
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    cfg_file = os.path.join(DATA_DIR, "razorpay.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                key_id = key_id or data.get("key_id", "").strip()
                key_secret = key_secret or data.get("key_secret", "").strip()
        except Exception:
            pass

    if key_id and key_secret:
        try:
            client = razorpay.Client(auth=(key_id, key_secret))
            return client, key_id
        except Exception as e:
            print("Error initializing Razorpay client:", e)
            return None, ""
    return None, key_id


@app.route("/api/payment/create-order", methods=["POST"])
def api_payment_create_order():
    """
    Creates a Razorpay Order for online payment.
    """
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    data = request.get_json(silent=True) or {}
    plan_key = data.get("plan_type", "test_30m")

    if plan_key not in SUBSCRIPTION_PLANS:
        return jsonify({"success": False, "error": "Invalid subscription plan selected."}), 400

    plan = SUBSCRIPTION_PLANS[plan_key]
    client, key_id = get_razorpay_client()

    if not client or not key_id:
        return jsonify({
            "success": False,
            "error": "Razorpay API keys not yet configured. Please add your Key ID and Secret in data/razorpay.json or contact founder Dheena (+91 7502399148).",
            "keys_missing": True
        }), 400

    conn = get_connection()
    store = conn.execute("SELECT store_code, store_name, owner_name, owner_email, owner_phone FROM stores WHERE id = ?", (store_id,)).fetchone()
    conn.close()

    receipt_id = f"rcpt_{store_id}_{int(time.time())}"[:40]

    try:
        order = client.order.create({
            "amount": plan["amount_paise"],
            "currency": "INR",
            "receipt": receipt_id,
            "notes": {
                "store_id": str(store_id),
                "store_code": store["store_code"] if store else "",
                "plan_name": plan["name"]
            }
        })

        # Record initiated order in DB
        record_payment_order(
            store_id=store_id,
            order_id=order["id"],
            amount=plan["price_rupees"],
            plan_name=plan["name"],
            duration_minutes=plan["duration_minutes"]
        )

        return jsonify({
            "success": True,
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": "INR",
            "key_id": key_id,
            "plan_name": plan["name"],
            "store_name": store["store_name"] if store else session.get("store_name", ""),
            "owner_name": store["owner_name"] if store else session.get("owner_name", ""),
            "owner_email": store["owner_email"] if store else "",
            "owner_phone": store["owner_phone"] if store else ""
        })
    except Exception as e:
        print("Razorpay order creation error:", e)
        return jsonify({"success": False, "error": f"Failed to create payment order: {str(e)}"}), 500


@app.route("/api/payment/verify", methods=["POST"])
def api_payment_verify():
    """
    Verifies Razorpay payment signature and automatically issues a unique, single-use license key.
    """
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    store_id = session.get("store_id")
    data = request.get_json(silent=True) or {}

    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    plan_key = data.get("plan_type", "test_30m")

    if not order_id or not payment_id or not signature:
        return jsonify({"success": False, "error": "Incomplete payment verification payload."}), 400

    plan = SUBSCRIPTION_PLANS.get(plan_key, SUBSCRIPTION_PLANS["test_30m"])
    client, _ = get_razorpay_client()

    if not client:
        return jsonify({"success": False, "error": "Payment gateway client unavailable."}), 500

    try:
        # Cryptographic HMAC SHA256 verification
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature
        })
    except Exception as e:
        print("Razorpay signature verification failed:", e)
        return jsonify({"success": False, "error": "Payment verification signature mismatch. Security check failed."}), 400

    # Payment is authentically verified!
    # 1. Generate strictly unique license key
    key_code = generate_license_key(
        duration_minutes=plan["duration_minutes"],
        price=plan["price_rupees"],
        plan_name=plan["name"]
    )

    # 2. Redeem it immediately for this store account
    redeem_ok, msg = redeem_license_key(key_code, store_id)

    # 3. Mark payment as paid in database
    record_payment_success(order_id, payment_id, signature, key_code, method="razorpay")

    sub = get_store_subscription(store_id)

    return jsonify({
        "success": True,
        "key_code": key_code,
        "payment_id": payment_id,
        "message": f"Payment of ₹{plan['price_rupees']} received! {plan['name']} unlocked successfully.",
        "expires_at": sub.get("expires_at"),
        "time_left_str": sub.get("time_left_str")
    })


# ==================================================
# LEGAL & COMPLIANCE PAGES (Required by Razorpay)
# ==================================================

@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/refund")
@app.route("/refund-policy")
def refund_page():
    return render_template("refund.html")


# ==================================================
# LOGOUT
# ==================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# ==================================================
# INITIALIZE DATABASE
# ==================================================

initialize_database()


# ==================================================
# START APPLICATION
# ==================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )   # development only - use launcher.py (or the .exe) for real use