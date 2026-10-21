"""
Founder Master Dashboard — Super Admin Panel for Dheena
========================================================
Only the founder (Dheena) can access this panel.
It provides a complete bird's-eye view of every store,
user, product, bill, payment, license key, and staff member
across the entire Cloud Billing SaaS platform.
"""

import os
import json
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, render_template, request, redirect, session, jsonify

from database import get_connection, get_store_subscription, generate_license_key, redeem_license_key
from paths import APP_DIR

DATA_DIR = os.path.join(APP_DIR, "data")

founder_bp = Blueprint("founder", __name__)


# ==================================================
# FOUNDER CREDENTIALS (Hardcoded + Configurable)
# ==================================================

FOUNDER_USERNAME = "dheena"
FOUNDER_PASSWORD = "Dheena@Founder2026"
FOUNDER_NAME = "Dheena"
FOUNDER_EMAIL = "deenadayalan779@gmail.com"

# Override from data/founder.json if present
_cfg = os.path.join(DATA_DIR, "founder.json")
if os.path.exists(_cfg):
    try:
        with open(_cfg, "r", encoding="utf-8") as f:
            _data = json.load(f)
            FOUNDER_USERNAME = _data.get("username", FOUNDER_USERNAME)
            FOUNDER_PASSWORD = _data.get("password", FOUNDER_PASSWORD)
    except Exception:
        pass


def founder_required(fn):
    """Decorator: only allow founder-authenticated sessions."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("founder_logged_in"):
            return redirect("/founder")
        return fn(*args, **kwargs)
    return wrapper


# ==================================================
# FOUNDER LOGIN / LOGOUT
# ==================================================

@founder_bp.route("/founder", methods=["GET", "POST"])
def founder_login():
    if session.get("founder_logged_in"):
        return redirect("/founder/dashboard")

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        if username == FOUNDER_USERNAME.lower() and password == FOUNDER_PASSWORD:
            session["founder_logged_in"] = True
            session["founder_name"] = FOUNDER_NAME
            return redirect("/founder/dashboard")
        else:
            error = "Invalid founder credentials."

    return render_template("founder_login.html", error=error)


@founder_bp.route("/founder/logout")
def founder_logout():
    session.pop("founder_logged_in", None)
    session.pop("founder_name", None)
    return redirect("/founder")


# ==================================================
# MASTER DASHBOARD
# ==================================================

@founder_bp.route("/founder/dashboard")
@founder_required
def founder_dashboard():
    return render_template("founder_dashboard.html")


# ==================================================
# API: PLATFORM OVERVIEW STATS
# ==================================================

@founder_bp.route("/founder/api/stats")
@founder_required
def founder_api_stats():
    conn = get_connection()
    try:
        total_stores = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        total_bills = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        total_revenue = conn.execute("SELECT COALESCE(SUM(total), 0) FROM bills").fetchone()[0]

        # Subscription stats
        active_pro = conn.execute("SELECT COUNT(*) FROM stores WHERE plan_status = 'active'").fetchone()[0]
        on_trial = conn.execute("SELECT COUNT(*) FROM stores WHERE plan_status = 'trial'").fetchone()[0]
        expired = conn.execute("SELECT COUNT(*) FROM stores WHERE plan_status = 'expired'").fetchone()[0]

        # Payment stats
        total_payments = conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'paid'").fetchone()[0]
        total_payment_revenue = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid'").fetchone()[0]

        # License keys
        total_keys = conn.execute("SELECT COUNT(*) FROM license_keys").fetchone()[0]
        used_keys = conn.execute("SELECT COUNT(*) FROM license_keys WHERE is_used = 1").fetchone()[0]

        # Staff
        total_staff = conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0]

        # Recent registrations (last 7 days)
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        new_stores_7d = conn.execute("SELECT COUNT(*) FROM stores WHERE created_at >= ?", (week_ago,)).fetchone()[0]

        # Today's bills
        today = datetime.now().strftime("%Y-%m-%d")
        today_bills = conn.execute("SELECT COUNT(*) FROM bills WHERE DATE(created_at) = ?", (today,)).fetchone()[0]
        today_revenue = conn.execute("SELECT COALESCE(SUM(total), 0) FROM bills WHERE DATE(created_at) = ?", (today,)).fetchone()[0]

        return jsonify({
            "success": True,
            "total_stores": total_stores,
            "total_users": total_users,
            "total_products": total_products,
            "total_bills": total_bills,
            "total_revenue": round(total_revenue, 2),
            "active_pro": active_pro,
            "on_trial": on_trial,
            "expired": expired,
            "total_payments": total_payments,
            "total_payment_revenue": round(total_payment_revenue, 2),
            "total_keys": total_keys,
            "used_keys": used_keys,
            "total_staff": total_staff,
            "new_stores_7d": new_stores_7d,
            "today_bills": today_bills,
            "today_revenue": round(today_revenue, 2)
        })
    finally:
        conn.close()


# ==================================================
# API: ALL STORES LIST
# ==================================================

@founder_bp.route("/founder/api/stores")
@founder_required
def founder_api_stores():
    conn = get_connection()
    try:
        stores = conn.execute("""
            SELECT 
                s.id, s.store_code, s.store_name, s.owner_name,
                s.owner_email, s.owner_phone,
                s.plan_status, s.trial_ends_at, s.subscription_ends_at,
                s.created_at,
                (SELECT COUNT(*) FROM users u WHERE u.store_id = s.id) as user_count,
                (SELECT COUNT(*) FROM products p WHERE p.store_id = s.id) as product_count,
                (SELECT COUNT(*) FROM bills b WHERE b.store_id = s.id) as bill_count,
                (SELECT COALESCE(SUM(b.total), 0) FROM bills b WHERE b.store_id = s.id) as total_revenue,
                (SELECT COUNT(*) FROM staff st WHERE st.store_id = s.id) as staff_count
            FROM stores s
            ORDER BY s.created_at DESC
        """).fetchall()

        result = []
        for s in stores:
            sub = get_store_subscription(s["id"])
            result.append({
                "id": s["id"],
                "store_code": s["store_code"],
                "store_name": s["store_name"],
                "owner_name": s["owner_name"],
                "owner_email": s["owner_email"] or "",
                "owner_phone": s["owner_phone"] or "",
                "plan_status": sub.get("plan_status", s["plan_status"]),
                "is_active": sub.get("is_active", False),
                "time_left_str": sub.get("time_left_str", ""),
                "expires_at": sub.get("expires_at", ""),
                "created_at": s["created_at"],
                "user_count": s["user_count"],
                "product_count": s["product_count"],
                "bill_count": s["bill_count"],
                "total_revenue": round(s["total_revenue"], 2),
                "staff_count": s["staff_count"]
            })

        return jsonify({"success": True, "stores": result})
    finally:
        conn.close()


# ==================================================
# API: SINGLE STORE DEEP DETAIL
# ==================================================

@founder_bp.route("/founder/api/store/<int:store_id>")
@founder_required
def founder_api_store_detail(store_id):
    conn = get_connection()
    try:
        store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not store:
            return jsonify({"success": False, "error": "Store not found"}), 404

        sub = get_store_subscription(store_id)

        # Users
        users = conn.execute("SELECT id, username, owner_name, role, email, phone, created_at FROM users WHERE store_id = ?", (store_id,)).fetchall()

        # Products (top 50)
        products = conn.execute("""
            SELECT id, name, billing_name, price, purchase_price, stock_quantity as stock, price_basis, barcode, discount_percent
            FROM products WHERE store_id = ? ORDER BY name LIMIT 50
        """, (store_id,)).fetchall()

        # Recent bills (last 20)
        bills = conn.execute("""
            SELECT id, bill_number, total, payment_status, payment_method, customer_name, created_at
            FROM bills WHERE store_id = ? ORDER BY created_at DESC LIMIT 20
        """, (store_id,)).fetchall()

        # Staff
        staff = conn.execute("SELECT id, name, phone, address, salary, salary_type, joining_date FROM staff WHERE store_id = ?", (store_id,)).fetchall()

        # Payments
        payments = conn.execute("""
            SELECT id, order_id, payment_id, amount, plan_name, duration_minutes, key_code, status, method, created_at
            FROM payments WHERE store_id = ? ORDER BY created_at DESC
        """, (store_id,)).fetchall()

        # License keys used by this store
        keys = conn.execute("""
            SELECT id, key_code, duration_minutes, price, plan_name, is_used, used_at, created_at
            FROM license_keys WHERE used_by_store_id = ? ORDER BY created_at DESC
        """, (store_id,)).fetchall()

        def rows_to_list(rows):
            return [dict(r) for r in rows] if rows else []

        return jsonify({
            "success": True,
            "store": {
                "id": store["id"],
                "store_code": store["store_code"],
                "store_name": store["store_name"],
                "owner_name": store["owner_name"],
                "owner_email": store["owner_email"] or "",
                "owner_phone": store["owner_phone"] or "",
                "plan_status": sub.get("plan_status"),
                "is_active": sub.get("is_active"),
                "time_left_str": sub.get("time_left_str"),
                "expires_at": sub.get("expires_at"),
                "created_at": store["created_at"]
            },
            "users": rows_to_list(users),
            "products": rows_to_list(products),
            "bills": rows_to_list(bills),
            "staff": rows_to_list(staff),
            "payments": rows_to_list(payments),
            "license_keys": rows_to_list(keys)
        })
    finally:
        conn.close()


# ==================================================
# API: ALL PAYMENTS
# ==================================================

@founder_bp.route("/founder/api/payments")
@founder_required
def founder_api_payments():
    conn = get_connection()
    try:
        payments = conn.execute("""
            SELECT p.*, s.store_name, s.store_code, s.owner_name
            FROM payments p
            LEFT JOIN stores s ON p.store_id = s.id
            ORDER BY p.created_at DESC
            LIMIT 200
        """).fetchall()
        return jsonify({"success": True, "payments": [dict(p) for p in payments]})
    finally:
        conn.close()


# ==================================================
# API: GENERATE LICENSE KEY (Founder Tool)
# ==================================================

@founder_bp.route("/founder/api/generate-key", methods=["POST"])
@founder_required
def founder_api_generate_key():
    data = request.get_json(silent=True) or {}
    duration = int(data.get("duration_minutes", 30))
    price = float(data.get("price", 5.0))
    plan = data.get("plan_name", f"{duration}-Minute Pro Access")

    key_code = generate_license_key(duration_minutes=duration, price=price, plan_name=plan)
    return jsonify({"success": True, "key_code": key_code, "duration_minutes": duration, "price": price})


# ==================================================
# API: ACTIVATE STORE SUBSCRIPTION (Founder Tool)
# ==================================================

@founder_bp.route("/founder/api/activate-store", methods=["POST"])
@founder_required
def founder_api_activate_store():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    duration = int(data.get("duration_minutes", 30))
    price = float(data.get("price", 5.0))
    plan = data.get("plan_name", f"{duration}-Minute Pro Access")

    if not store_id:
        return jsonify({"success": False, "error": "store_id is required."}), 400

    key_code = generate_license_key(duration_minutes=duration, price=price, plan_name=plan)
    ok, msg = redeem_license_key(key_code, store_id)

    if ok:
        sub = get_store_subscription(store_id)
        return jsonify({
            "success": True,
            "key_code": key_code,
            "message": msg,
            "expires_at": sub.get("expires_at"),
            "time_left_str": sub.get("time_left_str")
        })
    return jsonify({"success": False, "error": msg}), 400
