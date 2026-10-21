"""
Reports module.

All reports are calculated from the existing `bills`, `bill_items`
and `products` tables - no report data is stored separately.

Dates are compared in the server's LOCAL time (bills are stored in
UTC by SQLite's CURRENT_TIMESTAMP), so "today" means today on the
machine running the software.
"""

from datetime import datetime, date, timedelta

from flask import Blueprint, render_template, request, redirect, session, jsonify

from database import get_connection
from settings import get_settings


reports_bp = Blueprint("reports", __name__)

BILL_DATE = "DATE(b.created_at)"
PAYMENT_METHODS = ["Cash", "UPI", "Card", "Credit"]
MAX_RANGE_DAYS = 366


# ==================================================
# HELPERS
# ==================================================

def _parse_range():
    """Return (from_date, to_date) as date objects. Defaults to this month."""

    today = date.today()

    try:
        start = datetime.strptime(
            request.args.get("from_date", ""), "%Y-%m-%d"
        ).date()
    except ValueError:
        start = today.replace(day=1)

    try:
        end = datetime.strptime(
            request.args.get("to_date", ""), "%Y-%m-%d"
        ).date()
    except ValueError:
        end = today

    if start > end:
        start, end = end, start

    return start, end


def _guard():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if not session.get("store_id"):
        return jsonify({"error": "No store selected"}), 400

    return None


def _money(value):
    return round(float(value or 0), 2)


# ==================================================
# PAGE
# ==================================================

@reports_bp.route("/reports")
def reports_page():

    if not session.get("logged_in"):
        return redirect("/")

    if session.get("role") == "staff":
        return redirect("/billing")

    store_id = session.get("store_id")
    return render_template(
        "reports.html",
        default_low_stock=get_settings(store_id=store_id)["default_low_stock"]
    )


# ==================================================
# 1. SALES REPORT
# ==================================================

@reports_bp.route("/api/reports/sales")
def sales_report():

    denied = _guard()
    if denied:
        return denied

    start, end = _parse_range()
    store_id = session.get("store_id")

    conn = get_connection()

    try:

        params = (store_id, start.isoformat(), end.isoformat())

        summary = conn.execute(f"""
            SELECT
                COUNT(*)                 AS total_bills,
                COALESCE(SUM(b.total),0) AS total_sales
            FROM bills b
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
        """, params).fetchone()

        line_items = conn.execute(f"""
            SELECT COUNT(bi.id) AS line_items
            FROM bill_items bi
            JOIN bills b ON b.id = bi.bill_id
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
        """, params).fetchone()["line_items"]

        rows = conn.execute(f"""
            SELECT
                {BILL_DATE}              AS day,
                COUNT(*)                 AS bills,
                COALESCE(SUM(b.total),0) AS sales
            FROM bills b
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
            GROUP BY day
        """, params).fetchall()

        by_day = {r["day"]: r for r in rows}

        trend = []
        if (end - start).days <= MAX_RANGE_DAYS:
            day = start
            while day <= end:
                key = day.isoformat()
                row = by_day.get(key)
                trend.append({
                    "date": key,
                    "bills": int(row["bills"]) if row else 0,
                    "sales": _money(row["sales"]) if row else 0
                })
                day += timedelta(days=1)
        else:
            trend = [{
                "date": r["day"],
                "bills": int(r["bills"]),
                "sales": _money(r["sales"])
            } for r in rows]

        total_bills = int(summary["total_bills"])
        total_sales = _money(summary["total_sales"])

        return jsonify({
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "summary": {
                "total_sales": total_sales,
                "total_bills": total_bills,
                "line_items": int(line_items),
                "average_bill": round(
                    total_sales / total_bills, 2
                ) if total_bills else 0
            },
            "trend": trend
        })

    finally:
        conn.close()


# ==================================================
# 2. PRODUCT SALES REPORT
# ==================================================

@reports_bp.route("/api/reports/products")
def product_sales_report():

    denied = _guard()
    if denied:
        return denied

    start, end = _parse_range()
    store_id = session.get("store_id")

    conn = get_connection()

    try:

        rows = conn.execute(f"""
            SELECT
                bi.product_id,
                MAX(bi.product_name)        AS product_name,
                COALESCE(NULLIF(TRIM(MAX(bi.price_basis)), ''), 'unit') AS unit,
                SUM(bi.base_quantity)       AS quantity_sold,
                SUM(bi.total)               AS sales_amount,
                COUNT(DISTINCT bi.bill_id)  AS bills
            FROM bill_items bi
            JOIN bills b ON b.id = bi.bill_id
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
            GROUP BY bi.product_name, bi.price_basis
            ORDER BY sales_amount DESC
        """, (store_id, start.isoformat(), end.isoformat())).fetchall()

        products = [{
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "unit": r["unit"],
            "quantity_sold": round(float(r["quantity_sold"] or 0), 3),
            "sales_amount": _money(r["sales_amount"]),
            "bills": int(r["bills"])
        } for r in rows]

        return jsonify({
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "total_amount": round(
                sum(p["sales_amount"] for p in products), 2
            ),
            "products": products
        })

    finally:
        conn.close()


# ==================================================
# 3. STOCK REPORT  (current snapshot - no date range)
# ==================================================

@reports_bp.route("/api/reports/stock")
def stock_report():

    denied = _guard()
    if denied:
        return denied

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        default_level = float(get_settings(conn, store_id=store_id)["default_low_stock"])
    except (TypeError, ValueError):
        default_level = 10.0

    try:
        threshold = max(0.0, float(request.args.get("low_stock", default_level)))
    except ValueError:
        threshold = default_level

    try:

        rows = conn.execute("""
            SELECT id, name, billing_name, price_basis, price, stock_quantity
            FROM products
            WHERE store_id = ?
            ORDER BY name
        """, (store_id,)).fetchall()

        products = []
        counts = {"in": 0, "low": 0, "out": 0}
        inventory_value = 0.0

        for r in rows:

            stock = float(r["stock_quantity"] or 0)
            price = float(r["price"] or 0)

            if stock <= 0:
                status = "out"
            elif stock <= threshold:
                status = "low"
            else:
                status = "in"

            counts[status] += 1

            value = max(stock, 0) * price
            inventory_value += value

            products.append({
                "id": r["id"],
                "name": r["name"],
                "unit": r["price_basis"],
                "price": _money(price),
                "stock": round(stock, 3),
                "value": _money(value),
                "status": status
            })

        order = {"out": 0, "low": 1, "in": 2}
        products.sort(key=lambda p: (order[p["status"]], p["name"].lower()))

        return jsonify({
            "low_stock_threshold": threshold,
            "summary": {
                "total_products": len(products),
                "in_stock": counts["in"],
                "low_stock": counts["low"],
                "out_of_stock": counts["out"],
                "inventory_value": _money(inventory_value)
            },
            "products": products
        })

    finally:
        conn.close()


# ==================================================
# 4. CUSTOMER SALES REPORT
# ==================================================

@reports_bp.route("/api/reports/customers")
def customer_sales_report():

    denied = _guard()
    if denied:
        return denied

    start, end = _parse_range()
    search = request.args.get("search", "").strip()
    store_id = session.get("store_id")

    conn = get_connection()

    try:
        where_extra = ""
        params = [store_id, start.isoformat(), end.isoformat()]
        if search:
            where_extra = " AND (b.customer_name LIKE ? OR b.customer_phone LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        rows = conn.execute(f"""
            SELECT
                LOWER(TRIM(COALESCE(b.customer_name, ''))) AS customer_key,
                TRIM(COALESCE(b.customer_name, ''))        AS customer_name,
                COALESCE(b.customer_phone, '')             AS customer_phone,
                COUNT(*)                                   AS bills,
                COALESCE(SUM(b.total), 0)                  AS total_amount,
                COALESCE(SUM(CASE WHEN b.payment_status = 'Paid' THEN b.total ELSE 0 END), 0) AS total_paid,
                COALESCE(SUM(CASE WHEN b.payment_status != 'Paid' OR b.payment_status IS NULL THEN b.total ELSE 0 END), 0) AS total_unpaid,
                MAX(b.created_at)                          AS last_purchase
            FROM bills b
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?{where_extra}
            GROUP BY customer_key, b.customer_phone
            ORDER BY total_amount DESC
        """, params).fetchall()

        customers = [{
            "customer_name": r["customer_name"] or "Walk-in Customer",
            "customer_phone": r["customer_phone"] or "",
            "bills": int(r["bills"]),
            "total_amount": _money(r["total_amount"]),
            "total_paid": _money(r["total_paid"]),
            "total_unpaid": _money(r["total_unpaid"]),
            "last_purchase": r["last_purchase"]
        } for r in rows]

        named = [c for c in customers if c["customer_name"] and c["customer_name"] != "Walk-in Customer"]

        return jsonify({
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "unique_customers": len(named),
            "customers": customers
        })

    finally:
        conn.close()


# ==================================================
# 5. PAYMENT REPORT
# ==================================================

@reports_bp.route("/api/reports/payments")
def payment_report():

    denied = _guard()
    if denied:
        return denied

    start, end = _parse_range()
    store_id = session.get("store_id")
    conn = get_connection()

    ALL_METHODS = ["Cash", "UPI", "Card", "Credit", "Pending", "Loan", "Unpaid", "Not recorded"]

    try:
        params = (store_id, start.isoformat(), end.isoformat())
        # Get overall totals by method
        rows = conn.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(b.payment_method), ''), 'Not recorded') AS method,
                COUNT(*) AS bills,
                COALESCE(SUM(b.total), 0) AS amount
            FROM bills b
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
            GROUP BY method
        """, params).fetchall()

        found = {r["method"]: r for r in rows}

        # Get customer breakdown by method
        cust_rows = conn.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(b.payment_method), ''), 'Not recorded') AS method,
                COALESCE(NULLIF(TRIM(b.customer_name), ''), 'Walk-in Customer') AS customer_name,
                COALESCE(NULLIF(TRIM(b.customer_phone), ''), '') AS customer_phone,
                COUNT(*) AS bills_count,
                COALESCE(SUM(b.total), 0) AS amount
            FROM bills b
            WHERE b.store_id = ? AND {BILL_DATE} BETWEEN ? AND ?
            GROUP BY method, customer_name, customer_phone
            ORDER BY amount DESC
        """, params).fetchall()

        # Build nested dictionary of customers per method
        cust_dict = {m: [] for m in ALL_METHODS}
        # Include any unexpected methods dynamically
        for cr in cust_rows:
            m = cr["method"]
            if m not in cust_dict:
                cust_dict[m] = []
                ALL_METHODS.append(m)
                
            cust_dict[m].append({
                "customer_name": cr["customer_name"],
                "customer_phone": cr["customer_phone"],
                "bills_count": cr["bills_count"],
                "amount": round(float(cr["amount"]), 2)
            })

        methods = []
        for name in ALL_METHODS:
            row = found.get(name)
            if row or name in ["Cash", "UPI", "Card", "Credit", "Pending", "Loan", "Unpaid"]:
                methods.append({
                    "method": name,
                    "bills": int(row["bills"]) if row else 0,
                    "amount": _money(row["amount"]) if row else 0,
                    "customers": cust_dict.get(name, [])
                })

        collected = sum(
            m["amount"] for m in methods
            if m["method"] in ("Cash", "UPI", "Card")
        )

        pending = sum(
            m["amount"] for m in methods 
            if m["method"] in ("Credit", "Pending", "Loan", "Unpaid")
        )

        return jsonify({
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "total_collected": round(collected, 2),
            "pending": round(pending, 2),
            "methods": methods
        })

    finally:
        conn.close()


# ==================================================
# 6. CUSTOMER PROFILE REPORT
# ==================================================

@reports_bp.route("/api/reports/customer")
def customer_report():

    denied = _guard()
    if denied:
        return denied

    phone = request.args.get("phone", "").strip()
    name  = request.args.get("name", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date   = request.args.get("to_date", "").strip()
    exact = request.args.get("exact", "0") == "1"
    is_walkin = request.args.get("walkin", "0") == "1" or name.lower() in [
        "walk-in customer", "walkin customer", "walk-in", "walkin", "walk in customer"
    ]

    if not phone and not name and not is_walkin:
        return jsonify({"error": "Provide a phone number or customer name"}), 400

    store_id = session.get("store_id")
    conn = get_connection()

    try:
        # Build WHERE clause dynamically
        conditions = ["b.store_id = ?"]
        params = [store_id]

        if is_walkin:
            conditions.append("(b.customer_name IS NULL OR TRIM(b.customer_name) = '' OR LOWER(TRIM(b.customer_name)) LIKE '%walk%')")
        else:
            cust_conds = []
            if phone:
                if exact:
                    cust_conds.append("b.customer_phone = ?")
                    params.append(phone)
                else:
                    cust_conds.append("b.customer_phone LIKE ?")
                    params.append(f"%{phone}%")

            if name:
                if exact:
                    cust_conds.append("b.customer_name = ?")
                    params.append(name)
                else:
                    cust_conds.append("b.customer_name LIKE ?")
                    params.append(f"%{name}%")

            if cust_conds:
                conditions.append(f"({' OR '.join(cust_conds)})")

        if from_date and to_date:
            conditions.append(f"{BILL_DATE} BETWEEN ? AND ?")
            params.extend([from_date, to_date])

        where = " AND ".join(conditions)

        bills = conn.execute(f"""
            SELECT
                b.id,
                b.bill_number,
                b.customer_name,
                b.customer_phone,
                b.payment_method,
                COALESCE(b.payment_status, 'Paid') AS payment_status,
                b.total,
                COALESCE(b.tax_amount, 0) AS tax_amount,
                b.created_at
            FROM bills b
            WHERE {where}
            ORDER BY b.created_at DESC
        """, params).fetchall()

        bill_list = []
        total_paid = 0.0
        total_unpaid = 0.0

        for b in bills:
            status = b["payment_status"] or "Paid"
            total  = float(b["total"] or 0)

            if status == "Paid":
                total_paid += total
            else:
                total_unpaid += total

            # Fetch items for this bill
            items = conn.execute("""
                SELECT product_name, quantity, quantity_unit, price, total
                FROM bill_items
                WHERE bill_id = ?
            """, (b["id"],)).fetchall()

            bill_list.append({
                "id": b["id"],
                "bill_number": b["bill_number"],
                "customer_name": b["customer_name"],
                "customer_phone": b["customer_phone"] or "",
                "payment_method": b["payment_method"] or "",
                "payment_status": status,
                "total": round(total, 2),
                "tax_amount": round(float(b["tax_amount"]), 2),
                "created_at": b["created_at"],
                "items": [
                    {
                        "product_name": i["product_name"],
                        "quantity": float(i["quantity"]),
                        "quantity_unit": i["quantity_unit"],
                        "price": float(i["price"]),
                        "total": float(i["total"])
                    }
                    for i in items
                ]
            })

        ret_name = bills[0]["customer_name"] if (bills and bills[0]["customer_name"]) else ("Walk-in Customer" if is_walkin else name)
        ret_phone = bills[0]["customer_phone"] if bills else phone
        return jsonify({
            "customer_name": ret_name or "Customer",
            "customer_phone": ret_phone or "",
            "total_bills": len(bill_list),
            "total_paid": round(total_paid, 2),
            "total_unpaid": round(total_unpaid, 2),
            "total_amount": round(total_paid + total_unpaid, 2),
            "bills": bill_list
        })

    finally:
        conn.close()

