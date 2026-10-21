from database import get_connection


def get_invoice_data(bill_id, store_id=None):

    conn = get_connection()

    if store_id:
        bill = conn.execute(
            """
            SELECT *
            FROM bills
            WHERE id = ? AND (store_id = ? OR (? IS NULL AND store_id IS NULL))
            """,
            (bill_id, store_id, store_id)
        ).fetchone()
    else:
        bill = conn.execute(
            """
            SELECT *
            FROM bills
            WHERE id = ?
            """,
            (bill_id,)
        ).fetchone()

    if not bill:
        conn.close()
        return None

    items = conn.execute(
        """
        SELECT
            id,
            product_id,
            product_name,
            quantity,
            quantity_unit,
            base_quantity,
            price,
            price_basis,
            total
        FROM bill_items
        WHERE bill_id = ?
        ORDER BY id
        """,
        (bill_id,)
    ).fetchall()

    bill_store_id = bill["store_id"]
    business = None
    if bill_store_id:
        business = conn.execute(
            """
            SELECT *
            FROM business
            WHERE store_id = ?
            """,
            (bill_store_id,)
        ).fetchone()

    if not business:
        business = conn.execute(
            """
            SELECT *
            FROM business
            LIMIT 1
            """
        ).fetchone()

    conn.close()

    return {
        "bill": bill,
        "items": items,
        "business": business
    }