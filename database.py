import sqlite3
import os
import re
import secrets
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

from paths import APP_DIR


DATA_DIR = os.path.join(APP_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "billing.db")

# Older versions of this software stored everything in "easycode.db".
# If that file exists and the new one does not, keep the user's data
# by simply renaming it.
_LEGACY_DB_PATH = os.path.join(DATA_DIR, "easycode.db")

if os.path.exists(_LEGACY_DB_PATH) and not os.path.exists(DB_PATH):
    os.rename(_LEGACY_DB_PATH, DB_PATH)


def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")

    return conn


def initialize_database():

    conn = get_connection()
    cursor = conn.cursor()

    # =========================
    # STORES (Multi-Tenancy Master)
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_code TEXT UNIQUE NOT NULL,
            store_name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            owner_email TEXT,
            owner_phone TEXT,
            business_category TEXT DEFAULT '',
            plan_status TEXT DEFAULT 'trial',
            trial_ends_at TIMESTAMP,
            subscription_ends_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_code ON stores(store_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_email ON stores(owner_email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_phone ON stores(owner_phone)")

    # Migration for business_category
    cursor.execute("PRAGMA table_info(stores)")
    store_cols = [c["name"] for c in cursor.fetchall()]
    if "business_category" not in store_cols:
        cursor.execute("ALTER TABLE stores ADD COLUMN business_category TEXT DEFAULT ''")

    # =========================
    # LICENSE KEYS (Single-Use, Store-Unique)
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_code TEXT UNIQUE NOT NULL,
            duration_minutes INTEGER DEFAULT 30,
            price REAL DEFAULT 5.0,
            plan_name TEXT DEFAULT 'Test Pro (30 Mins)',
            is_used INTEGER DEFAULT 0,
            used_by_store_id INTEGER,
            used_by_store_code TEXT,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (used_by_store_id) REFERENCES stores(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_license_keys_code ON license_keys(key_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_license_keys_store ON license_keys(used_by_store_id)")

    # =========================
    # PAYMENTS (Razorpay & Subscription Transactions)
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL,
            order_id TEXT NOT NULL,
            payment_id TEXT UNIQUE,
            signature TEXT,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'INR',
            plan_name TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            key_code TEXT,
            status TEXT DEFAULT 'created',
            method TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (store_id) REFERENCES stores(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_payment ON payments(payment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_store ON payments(store_id)")

    # =========================
    # USERS
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            owner_name TEXT,
            role TEXT NOT NULL DEFAULT 'admin',
            email TEXT,
            phone TEXT,
            staff_id INTEGER,
            email_verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
        )
    """)

    # =========================
    # BUSINESS
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS business (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,

            store_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            postal_code TEXT,

            phone TEXT,
            email TEXT,
            gstin TEXT,

            invoice_prefix TEXT DEFAULT 'INV',
            invoice_start_number INTEGER DEFAULT 1,
            invoice_footer TEXT,

            setup_completed INTEGER DEFAULT 0
        )
    """)

    # =========================
    # BILLS
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bills (

            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,

            bill_number TEXT NOT NULL,

            total REAL NOT NULL,

            discount_percent REAL DEFAULT 0,

            discount_amount REAL DEFAULT 0,

            flat_discount REAL DEFAULT 0,

            created_at
                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE (store_id, bill_number)
        )
    """)
    
    # =========================
    # STOCK MOVEMENTS
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            product_id INTEGER NOT NULL,

            movement_type TEXT NOT NULL,

            quantity REAL NOT NULL,

            stock_before REAL NOT NULL,

            stock_after REAL NOT NULL,

            reason TEXT,

            created_at
                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (product_id)
                REFERENCES products(id)
        )
    """)


    # =========================
    # PRODUCTS
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (

            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,

            name TEXT NOT NULL,

            billing_name TEXT NOT NULL,

            price_basis TEXT NOT NULL
                DEFAULT 'Piece',

            price REAL NOT NULL
                DEFAULT 0,

            purchase_price REAL NOT NULL
                DEFAULT 0,

            barcode TEXT DEFAULT '',

            discount_percent REAL DEFAULT 0,

            stock_quantity REAL NOT NULL
                DEFAULT 0,

            created_at
                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE (store_id, name)
        )
    """)

    # =========================
    # STAFF & ATTENDANCE
    # =========================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,
            name TEXT NOT NULL,
            phone TEXT,
            address TEXT,
            salary REAL DEFAULT 0,
            salary_type TEXT DEFAULT 'Monthly',
            joining_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            clock_in TEXT,
            clock_out TEXT,
            status TEXT DEFAULT 'Present',
            hours_worked REAL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_extra_pay (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            pay_type TEXT DEFAULT 'Extra Pay',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_allowances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    # Auto-migrations for staff & attendance tables
    cursor.execute("PRAGMA table_info(staff)")
    staff_cols = [c["name"] for c in cursor.fetchall()]
    if "store_id" not in staff_cols:
        cursor.execute("ALTER TABLE staff ADD COLUMN store_id INTEGER")
    if "salary" not in staff_cols:
        cursor.execute("ALTER TABLE staff ADD COLUMN salary REAL DEFAULT 0")
    if "salary_type" not in staff_cols:
        cursor.execute("ALTER TABLE staff ADD COLUMN salary_type TEXT DEFAULT 'Monthly'")
    if "joining_date" not in staff_cols:
        cursor.execute("ALTER TABLE staff ADD COLUMN joining_date TEXT")

    cursor.execute("PRAGMA table_info(staff_attendance)")
    att_cols = [c["name"] for c in cursor.fetchall()]
    if "hours_worked" not in att_cols:
        cursor.execute("ALTER TABLE staff_attendance ADD COLUMN hours_worked REAL DEFAULT 0")
    if "notes" not in att_cols:
        cursor.execute("ALTER TABLE staff_attendance ADD COLUMN notes TEXT")

    # ==================================================
    # PRODUCT DATABASE MIGRATION
    # ==================================================

    cursor.execute("""
        PRAGMA table_info(products)
    """)

    existing_columns = [
        column["name"]
        for column in cursor.fetchall()
    ]

    # Add billing_name if old database
    # does not have it.

    if "billing_name" not in existing_columns:

        cursor.execute("""
            ALTER TABLE products
            ADD COLUMN billing_name TEXT
        """)

    # Add price_basis if old database
    # does not have it.

    if "price_basis" not in existing_columns:

        cursor.execute("""
            ALTER TABLE products
            ADD COLUMN price_basis TEXT
                DEFAULT 'Piece'
        """)

    # Add purchase_price (cost/invested price) if missing
    if "purchase_price" not in existing_columns:

        cursor.execute("""
            ALTER TABLE products
            ADD COLUMN purchase_price REAL
                DEFAULT 0
        """)

    if "barcode" not in existing_columns:

        cursor.execute("""
            ALTER TABLE products
            ADD COLUMN barcode TEXT DEFAULT ''
        """)

    if "discount_percent" not in existing_columns:

        cursor.execute("""
            ALTER TABLE products
            ADD COLUMN discount_percent REAL DEFAULT 0
        """)

    # ==================================================
    # UPDATE OLD PRODUCTS
    # ==================================================

    cursor.execute("""
        UPDATE products

        SET billing_name = name

        WHERE billing_name IS NULL
           OR billing_name = ''
    """)

    cursor.execute("""
        UPDATE products

        SET price_basis = 'Piece'

        WHERE price_basis IS NULL
           OR price_basis = ''
    """)

    # ==================================================
    # USERS DATABASE MIGRATION
    # ==================================================

    cursor.execute("""
        PRAGMA table_info(users)
    """)

    user_columns = [
        column["name"]
        for column in cursor.fetchall()
    ]

    if "owner_name" not in user_columns:

        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN owner_name TEXT
        """)

    if "role" not in user_columns:

        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'
        """)

    if "staff_id" not in user_columns:

        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN staff_id INTEGER
        """)

    if "email_verified" not in user_columns:

        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN email_verified INTEGER DEFAULT 0
        """)

    # ==================================================
    # BUSINESS DATABASE MIGRATION
    # ==================================================

    cursor.execute("""
        PRAGMA table_info(business)
    """)

    business_columns = [
        column["name"]
        for column in cursor.fetchall()
    ]

    business_new_columns = {

        "city": "TEXT",

        "state": "TEXT",

        "postal_code": "TEXT",

        "email": "TEXT",

        "invoice_prefix": "TEXT DEFAULT 'INV'",

        "invoice_start_number": "INTEGER DEFAULT 1",

        "invoice_footer": "TEXT",

        "setup_completed": "INTEGER DEFAULT 0",

        # ---- Settings V1 ----
        "logo": "TEXT",
        "show_logo": "INTEGER DEFAULT 1",
        "default_low_stock": "REAL DEFAULT 10",
        "payment_methods": "TEXT DEFAULT 'Cash,UPI,Card,Credit'",
        "require_customer_name": "INTEGER DEFAULT 1",

        # ---- Settings V2 ----
        "paper_size": "TEXT DEFAULT 'A4'",
        "auto_print": "INTEGER DEFAULT 0",
        "gst_enabled": "INTEGER DEFAULT 0",
        "gst_rate": "REAL DEFAULT 18",
        "session_timeout": "INTEGER DEFAULT 480",
        "currency_symbol": "TEXT DEFAULT '₹'",
        "date_format": "TEXT DEFAULT 'DD-MM-YYYY'"
    }

    for column_name, column_type in business_new_columns.items():

        if column_name not in business_columns:

            cursor.execute(
                f"""
                ALTER TABLE business
                ADD COLUMN {column_name} {column_type}
                """
            )

    # ==================================================
    # BILLS DATABASE MIGRATION (customer name)
    # ==================================================

    cursor.execute("PRAGMA table_info(bills)")

    bill_columns = [
        column["name"]
        for column in cursor.fetchall()
    ]

    if "customer_name" not in bill_columns:
        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN customer_name TEXT DEFAULT ''
        """)

    if "customer_phone" not in bill_columns:
        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN customer_phone TEXT DEFAULT ''
        """)

    if "payment_status" not in bill_columns:
        # Paid or Unpaid
        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN payment_status TEXT DEFAULT 'Paid'
        """)

    if "payment_method" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN payment_method TEXT
        """)

    # GST snapshot taken when the bill is created, so changing the
    # GST setting later never alters old invoices.
    if "tax_rate" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN tax_rate REAL DEFAULT 0
        """)

    if "tax_amount" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN tax_amount REAL DEFAULT 0
        """)

    if "discount_percent" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN discount_percent REAL DEFAULT 0
        """)

    if "discount_amount" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN discount_amount REAL DEFAULT 0
        """)

    if "flat_discount" not in bill_columns:

        cursor.execute("""
            ALTER TABLE bills
            ADD COLUMN flat_discount REAL DEFAULT 0
        """)

    # ==================================================
    # BILL ITEMS
    # ==================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bill_items (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            bill_id INTEGER NOT NULL,

            product_id INTEGER,

            product_name TEXT NOT NULL,

            quantity REAL NOT NULL,

            quantity_unit TEXT NOT NULL,

            base_quantity REAL NOT NULL,

            price REAL NOT NULL,

            price_basis TEXT NOT NULL,

            total REAL NOT NULL,

            FOREIGN KEY (bill_id)
                REFERENCES bills(id),

            FOREIGN KEY (product_id)
                REFERENCES products(id)
        )
    """)

    # ==================================================
    # BILL ITEMS MIGRATION (allow NULL product_id)
    # ==================================================

    cursor.execute("PRAGMA table_info(bill_items)")
    bill_items_cols = {col["name"]: col for col in cursor.fetchall()}
    
    if bill_items_cols.get("product_id") and bill_items_cols["product_id"]["notnull"] == 1:
        cursor.execute("ALTER TABLE bill_items RENAME TO old_bill_items")
        cursor.execute("""
            CREATE TABLE bill_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id INTEGER NOT NULL,
                product_id INTEGER,
                product_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                quantity_unit TEXT NOT NULL,
                base_quantity REAL NOT NULL,
                price REAL NOT NULL,
                price_basis TEXT NOT NULL,
                total REAL NOT NULL,
                FOREIGN KEY (bill_id) REFERENCES bills(id),
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        """)
        cursor.execute("""
            INSERT INTO bill_items (id, bill_id, product_id, product_name, quantity, quantity_unit, base_quantity, price, price_basis, total)
            SELECT id, bill_id, product_id, product_name, quantity, quantity_unit, base_quantity, price, price_basis, total FROM old_bill_items
        """)
        cursor.execute("DROP TABLE old_bill_items")

    # ==================================================
    # BILL ITEMS COST PRICE MIGRATION
    # ==================================================
    cursor.execute("PRAGMA table_info(bill_items)")
    bill_items_cols = {col["name"]: col for col in cursor.fetchall()}
    if "cost_price" not in bill_items_cols:
        cursor.execute("ALTER TABLE bill_items ADD COLUMN cost_price REAL DEFAULT 0")

    # ==================================================
    # PRODUCT BATCHES (Purchase & refill history)
    # ==================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            purchase_date TEXT NOT NULL,
            quantity_added REAL NOT NULL,
            quantity_remaining REAL NOT NULL,
            invested_price REAL NOT NULL,
            selling_price REAL NOT NULL,
            discount_percent REAL DEFAULT 0,
            supplier_note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("PRAGMA table_info(product_batches)")
    batch_cols = [c["name"] for c in cursor.fetchall()]
    if "discount_percent" not in batch_cols:
        cursor.execute("ALTER TABLE product_batches ADD COLUMN discount_percent REAL DEFAULT 0")

    # ==================================================
    # BATCH DEDUCTIONS (Tracks exact FIFO deductions & per-batch discounts per bill)
    # ==================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batch_deductions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            bill_item_id INTEGER,
            product_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            unit_selling_price REAL NOT NULL,
            unit_cost_price REAL NOT NULL,
            cart_discount_allocated REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES product_batches(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batch_deductions_batch ON batch_deductions(batch_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batch_deductions_bill ON batch_deductions(bill_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batch_deductions_product ON batch_deductions(product_id)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_product ON product_batches(product_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_active ON product_batches(product_id, quantity_remaining, purchase_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bills_created_at ON bills(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bills_payment_status ON bills(payment_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bills_customer_name ON bills(customer_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bills_customer_phone ON bills(customer_phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bill_items_bill_id ON bill_items(bill_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bill_items_product_id ON bill_items(product_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id)")

    # Seed batches for existing products that have stock
    cursor.execute("""
        INSERT INTO product_batches (product_id, purchase_date, quantity_added, quantity_remaining, invested_price, selling_price, supplier_note)
        SELECT 
            p.id,
            COALESCE(DATE(p.created_at), DATE('now')),
            p.stock_quantity,
            p.stock_quantity,
            COALESCE(p.purchase_price, 0),
            COALESCE(p.price, 0),
            'Initial stock'
        FROM products p
        WHERE p.id NOT IN (SELECT DISTINCT product_id FROM product_batches)
          AND p.stock_quantity > 0
    """)

    # Multi-Tenant Migrations: Add store_id to all operational tables
    tables_to_tenant = [
        "users", "business", "products", "product_batches",
        "bills", "bill_items", "batch_deductions", "stock_movements",
        "staff", "staff_attendance", "staff_extra_pay", "staff_allowances"
    ]
    for tbl in tables_to_tenant:
        cursor.execute(f"PRAGMA table_info({tbl})")
        cols = [c["name"] for c in cursor.fetchall()]
        if cols and "store_id" not in cols:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN store_id INTEGER")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_store_id ON {tbl}(store_id)")

    # Also ensure users table has email and phone columns
    cursor.execute("PRAGMA table_info(users)")
    u_cols = [c["name"] for c in cursor.fetchall()]
    if "email" not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "phone" not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT")

    # ==================================================
    # COMMIT
    # ==================================================

    conn.commit()
    conn.close()


def register_new_store(store_name, owner_name, email_or_phone, password, city="", state="", address="", business_type="", custom_store_code=None, email_verified=0):
    """
    Registers a new store with a 5-minute free trial.
    Creates:
      1. Store in `stores` table
      2. Admin user in `users` table
      3. Business profile in `business` table
    Returns dict with store_id, store_code, user_id, username, business_category.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        store_name = store_name.strip()
        owner_name = owner_name.strip()
        email_or_phone = email_or_phone.strip()
        city = (city or "").strip()
        state = (state or "").strip()
        address = (address or "").strip()
        business_type = (business_type or "").strip()

        is_email = "@" in email_or_phone
        email = email_or_phone if is_email else ""
        phone = "" if is_email else email_or_phone

        # Store Code Logic: custom if provided, otherwise Character-Number combination (e.g. MALAR-101)
        if custom_store_code and custom_store_code.strip():
            candidate = re.sub(r"[^A-Za-z0-9\-]", "", custom_store_code.strip()).upper()
            row = conn.execute("SELECT id FROM stores WHERE UPPER(store_code) = ?", (candidate,)).fetchone()
            if row:
                raise ValueError("Store code already exists. Please choose another store code.")
            store_code = candidate
        else:
            # Generate unique Character-Number code (e.g. MALAR-101)
            clean_code = re.sub(r"[^A-Za-z0-9]", "", store_name).upper()[:6]
            if len(clean_code) < 3:
                clean_code = "STORE"

            attempt = 101
            while True:
                store_code = f"{clean_code}-{attempt}"
                row = conn.execute("SELECT id FROM stores WHERE store_code = ?", (store_code,)).fetchone()
                if not row:
                    break
                attempt += 1

        # 7-day free trial for new store registrations
        trial_ends = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("""
            INSERT INTO stores (
                store_code, store_name, owner_name, owner_email, owner_phone,
                business_category, plan_status, trial_ends_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'trial', ?)
        """, (store_code, store_name, owner_name, email, phone, business_type, trial_ends))
        store_id = cur.lastrowid

        # Create Admin user
        hashed_pw = generate_password_hash(password)
        username = email or phone or f"admin_{store_code.lower()}"
        cur_u = conn.execute("""
            INSERT INTO users (store_id, username, password, owner_name, role, email, phone, email_verified)
            VALUES (?, ?, ?, ?, 'admin', ?, ?, ?)
        """, (store_id, username, hashed_pw, owner_name, email, phone, 1 if (not email or email_verified) else 0))
        user_id = cur_u.lastrowid

        # Create Business profile
        conn.execute("""
            INSERT INTO business (
                store_id, store_name, address, city, state, email, phone, invoice_prefix,
                invoice_start_number, setup_completed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'INV', 1, 1)
        """, (store_id, store_name, address, city, state, email, phone))

        conn.commit()
        return {
            "success": True,
            "store_id": store_id,
            "store_code": store_code,
            "store_name": store_name,
            "business_category": business_type,
            "user_id": user_id,
            "username": username,
            "owner_name": owner_name
        }
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_store_subscription(store_id):
    """
    Check subscription and trial status for a store.
    Supports minute-level precision for testing (5 min trial, 30 min Pro).
    Returns dict:
      is_active: bool
      plan_status: 'trial' | 'active' | 'expired'
      seconds_left: int
      minutes_left: int
      days_left: int
      time_left_str: str (e.g. '4m 32s', '28m 10s', '12 days')
      expires_at: str (e.g. '21 Oct 2026, 09:15 PM')
      store_code: str
      store_name: str
    """
    if not store_id:
        return {
            "is_active": False,
            "plan_status": "expired",
            "seconds_left": 0,
            "minutes_left": 0,
            "days_left": 0,
            "time_left_str": "Expired",
            "expires_at": None,
            "store_code": "",
            "store_name": "",
            "business_category": ""
        }

    conn = get_connection()
    try:
        store = conn.execute(
            "SELECT id, store_code, store_name, plan_status, trial_ends_at, subscription_ends_at, business_category FROM stores WHERE id = ?",
            (store_id,)
        ).fetchone()

        if not store:
            return {
                "is_active": False,
                "plan_status": "expired",
                "seconds_left": 0,
                "minutes_left": 0,
                "days_left": 0,
                "time_left_str": "Expired",
                "expires_at": None,
                "store_code": "",
                "store_name": "",
                "business_category": ""
            }

        b_cat = (store["business_category"] or "") if "business_category" in store.keys() else ""

        now = datetime.now()
        plan_status = store["plan_status"] or "trial"
        trial_ends = None
        sub_ends = None

        if store["trial_ends_at"]:
            try:
                trial_ends = datetime.strptime(str(store["trial_ends_at"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if store["subscription_ends_at"]:
            try:
                sub_ends = datetime.strptime(str(store["subscription_ends_at"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        def calc_remaining(target_dt):
            diff = int((target_dt - now).total_seconds())
            if diff <= 0:
                return 0, 0, 0, "Expired"
            days = diff // 86400
            mins = diff // 60
            secs = diff % 60
            if diff < 3600:
                time_str = f"{mins}m {secs}s"
            elif diff < 86400:
                hours = diff // 3600
                rem_mins = (diff % 3600) // 60
                time_str = f"{hours}h {rem_mins}m"
            else:
                time_str = f"{days} days"
            return diff, mins, days, time_str

        # Case 1: Active Pro Subscription
        if plan_status == "active":
            if sub_ends:
                sec_left, mins_left, days_left, time_str = calc_remaining(sub_ends)
                if sec_left <= 0:
                    conn.execute("UPDATE stores SET plan_status = 'expired' WHERE id = ?", (store_id,))
                    conn.commit()
                    return {
                        "is_active": False,
                        "plan_status": "expired",
                        "seconds_left": 0,
                        "minutes_left": 0,
                        "days_left": 0,
                        "time_left_str": "Expired",
                        "expires_at": sub_ends.strftime("%d %b %Y, %I:%M %p"),
                        "store_code": store["store_code"],
                        "store_name": store["store_name"],
                        "business_category": b_cat
                    }
                else:
                    return {
                        "is_active": True,
                        "plan_status": "active",
                        "seconds_left": sec_left,
                        "minutes_left": mins_left,
                        "days_left": days_left,
                        "time_left_str": time_str,
                        "expires_at": sub_ends.strftime("%d %b %Y, %I:%M %p"),
                        "store_code": store["store_code"],
                        "store_name": store["store_name"],
                        "business_category": b_cat
                    }
            else:
                return {
                    "is_active": True,
                    "plan_status": "active",
                    "seconds_left": 99999999,
                    "minutes_left": 999999,
                    "days_left": 999,
                    "time_left_str": "Lifetime",
                    "expires_at": "Lifetime Pro",
                    "store_code": store["store_code"],
                    "store_name": store["store_name"],
                    "business_category": b_cat
                }

        # Case 2: Free Trial (5 minutes)
        if plan_status == "trial":
            if trial_ends:
                sec_left, mins_left, days_left, time_str = calc_remaining(trial_ends)
                if sec_left <= 0:
                    conn.execute("UPDATE stores SET plan_status = 'expired' WHERE id = ?", (store_id,))
                    conn.commit()
                    return {
                        "is_active": False,
                        "plan_status": "expired",
                        "seconds_left": 0,
                        "minutes_left": 0,
                        "days_left": 0,
                        "time_left_str": "Expired",
                        "expires_at": trial_ends.strftime("%d %b %Y, %I:%M %p"),
                        "store_code": store["store_code"],
                        "store_name": store["store_name"],
                        "business_category": b_cat
                    }
                else:
                    return {
                        "is_active": True,
                        "plan_status": "trial",
                        "seconds_left": sec_left,
                        "minutes_left": mins_left,
                        "days_left": days_left,
                        "time_left_str": time_str,
                        "expires_at": trial_ends.strftime("%d %b %Y, %I:%M %p"),
                        "store_code": store["store_code"],
                        "store_name": store["store_name"],
                        "business_category": b_cat
                    }
            else:
                return {
                    "is_active": True,
                    "plan_status": "trial",
                    "seconds_left": 300,
                    "minutes_left": 5,
                    "days_left": 0,
                    "time_left_str": "5m 00s",
                    "expires_at": "In 5 Minutes",
                    "store_code": store["store_code"],
                    "store_name": store["store_name"],
                    "business_category": b_cat
                }

        # Case 3: Already expired
        exp_target = sub_ends or trial_ends or now
        return {
            "is_active": False,
            "plan_status": "expired",
            "seconds_left": 0,
            "minutes_left": 0,
            "days_left": 0,
            "time_left_str": "Expired",
            "expires_at": exp_target.strftime("%d %b %Y, %I:%M %p"),
            "store_code": store["store_code"],
            "store_name": store["store_name"],
            "business_category": b_cat
        }
    finally:
        conn.close()


def generate_license_key(duration_minutes=30, price=5.0, plan_name="Test Pro (30 Mins)"):
    """
    Generates a cryptographically unique license key and saves it as unused.
    Format: PRO-30M-XXXX-XXXX
    """
    conn = get_connection()
    try:
        while True:
            t1 = secrets.token_hex(2).upper()
            t2 = secrets.token_hex(2).upper()
            prefix = "PRO-30M" if duration_minutes == 30 else f"PRO-{duration_minutes}M"
            key_code = f"{prefix}-{t1}-{t2}"
            exists = conn.execute("SELECT id FROM license_keys WHERE UPPER(key_code) = ?", (key_code,)).fetchone()
            if not exists:
                break

        conn.execute("""
            INSERT INTO license_keys (key_code, duration_minutes, price, plan_name, is_used)
            VALUES (?, ?, ?, ?, 0)
        """, (key_code, duration_minutes, price, plan_name))
        conn.commit()
        return key_code
    finally:
        conn.close()


def redeem_license_key(key_code, store_id):
    """
    Redeems a license key strictly for a single store account.
    If the key is already used, it CANNOT be redeemed again by anyone.
    """
    if not key_code or not store_id:
        return False, "License key and store account are required."

    clean_key = str(key_code).strip().upper()
    conn = get_connection()
    try:
        key_row = conn.execute(
            "SELECT id, key_code, duration_minutes, price, plan_name, is_used, used_by_store_id, used_by_store_code FROM license_keys WHERE UPPER(key_code) = ?",
            (clean_key,)
        ).fetchone()

        if not key_row:
            return False, "Invalid license key. Please check the code or contact founder Dheena (+91 7502399148)."

        if key_row["is_used"] == 1:
            if key_row["used_by_store_id"] == store_id:
                return False, "This license key has already been activated on your store account."
            else:
                return False, "This license key has already been used by another store. Every key is strictly single-use only."

        store = conn.execute("SELECT id, store_code, store_name, subscription_ends_at FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not store:
            return False, "Store account not found."

        now = datetime.now()
        base_time = now
        if store["subscription_ends_at"]:
            try:
                curr_end = datetime.strptime(str(store["subscription_ends_at"])[:19], "%Y-%m-%d %H:%M:%S")
                if curr_end > now:
                    base_time = curr_end
            except Exception:
                pass

        duration_mins = key_row["duration_minutes"] or 30
        new_end = (base_time + timedelta(minutes=duration_mins)).strftime("%Y-%m-%d %H:%M:%S")

        # Mark key used permanently
        conn.execute("""
            UPDATE license_keys 
            SET is_used = 1, used_by_store_id = ?, used_by_store_code = ?, used_at = ?
            WHERE id = ?
        """, (store["id"], store["store_code"], now.strftime("%Y-%m-%d %H:%M:%S"), key_row["id"]))

        # Extend store subscription
        conn.execute("""
            UPDATE stores 
            SET plan_status = 'active', subscription_ends_at = ?
            WHERE id = ?
        """, (new_end, store["id"]))

        conn.commit()
        return True, f"Pro unlocked successfully! Valid for {duration_mins} minutes until {new_end}."
    except Exception as e:
        conn.rollback()
        return False, f"Failed to activate key: {str(e)}"
    finally:
        conn.close()


def process_test_payment(store_id, amount=5.0, duration_minutes=30):
    """
    Processes ₹5 test payment for 30 minutes of Pro access.
    Generates a unique license key, saves it, redeems it for this store,
    and returns (success, key_code, message).
    """
    conn = get_connection()
    try:
        store = conn.execute("SELECT id, store_code, store_name FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not store:
            return False, None, "Store account not found."

        # 1. Generate unique single-use key
        key_code = generate_license_key(duration_minutes=duration_minutes, price=amount, plan_name="Test Pro (30 Mins)")

        # 2. Redeem it for this store
        success, msg = redeem_license_key(key_code, store_id)
        return success, key_code, msg
    finally:
        conn.close()


def record_payment_order(store_id, order_id, amount, plan_name, duration_minutes):
    """
    Records an initiated Razorpay payment order.
    """
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO payments (store_id, order_id, amount, plan_name, duration_minutes, status)
            VALUES (?, ?, ?, ?, ?, 'created')
        """, (store_id, order_id, amount, plan_name, duration_minutes))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_payment_success(order_id, payment_id, signature, key_code, method=""):
    """
    Marks a Razorpay payment order as successfully paid.
    """
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE payments
            SET payment_id = ?, signature = ?, key_code = ?, status = 'paid', method = ?
            WHERE order_id = ?
        """, (payment_id, signature, key_code, method, order_id))
        conn.commit()
    finally:
        conn.close()


def activate_store_subscription(store_id_or_code, duration_minutes=30):
    """
    Activates or extends a store's Pro subscription by duration_minutes.
    """
    conn = get_connection()
    try:
        if isinstance(store_id_or_code, int) or str(store_id_or_code).isdigit():
            store = conn.execute("SELECT id, store_code, subscription_ends_at FROM stores WHERE id = ?", (int(store_id_or_code),)).fetchone()
        else:
            code = str(store_id_or_code).strip().upper()
            store = conn.execute("SELECT id, store_code, subscription_ends_at FROM stores WHERE UPPER(store_code) = ?", (code,)).fetchone()

        if not store:
            return False, "Store not found"

        now = datetime.now()
        base_time = now
        if store["subscription_ends_at"]:
            try:
                curr_end = datetime.strptime(str(store["subscription_ends_at"])[:19], "%Y-%m-%d %H:%M:%S")
                if curr_end > now:
                    base_time = curr_end
            except Exception:
                pass

        new_end = (base_time + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE stores SET plan_status = 'active', subscription_ends_at = ? WHERE id = ?",
            (new_end, store["id"])
        )
        conn.commit()
        return True, f"Pro activated successfully until {new_end}"
    finally:
        conn.close()

