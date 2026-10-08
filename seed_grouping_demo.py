"""
Adds 5 PACKED orders to the seller "Demo Big Mart" so you can test Smart Delivery Grouping.

    python seed_grouping_demo.py

Run seed_demo.py first (it creates Demo Big Mart and the demo customer).
You can run this script several times to get more packed orders.
Orders go to: Colony A, Colony A, Colony B, Colony C, Colony D.
To remove everything, run:  python seed_demo.py clear

The admin dashboard also has a button that does the same thing.
"""
from db import get_connection
from localities import LOCALITIES

SELLER_EMAIL = "demo.bigmart@example.com"
CUSTOMER_EMAIL = "demo.customer@example.com"
DESTINATIONS = ["Colony A", "Colony A", "Colony B", "Colony C", "Colony D"]


def add_packed_orders(cursor):
    """Adds the 5 packed orders. Returns how many were added; raises ValueError if the demo data is missing."""
    cursor.execute("SELECT seller_id FROM sellers WHERE email = %s", (SELLER_EMAIL,))
    seller = cursor.fetchone()
    cursor.execute("SELECT customer_id FROM customers WHERE email = %s", (CUSTOMER_EMAIL,))
    customer = cursor.fetchone()
    if not seller or not customer:
        raise ValueError("Demo seller or customer not found. Reset the demo data first.")

    cursor.execute("SELECT product_id, price FROM products WHERE seller_id = %s LIMIT 1", (seller[0],))
    product_id, price = cursor.fetchone()

    for colony in DESTINATIONS:
        lat, lon = LOCALITIES[colony]
        cursor.execute(
            "INSERT INTO orders (customer_id, seller_id, total_amount, delivery_address, "
            "delivery_latitude, delivery_longitude, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'packed')",
            (customer[0], seller[0], price * 2, "House 7, " + colony, lat, lon),
        )
        order_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (%s, %s, 2, %s)",
            (order_id, product_id, price),
        )
        print("Created packed order #" + str(order_id) + " going to " + colony)
    return len(DESTINATIONS)


def main():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        add_packed_orders(cursor)
        conn.commit()
        print("Done. Log in as demo.bigmart@example.com / demo123 and open 'Delivery groups'.")
    except ValueError as error:
        print(error)
        raise SystemExit(1)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()