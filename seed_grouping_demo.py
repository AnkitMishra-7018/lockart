"""
Adds 5 PACKED orders to the seller "Demo Big Mart" so you can test Smart Delivery Grouping.

    python seed_grouping_demo.py

Run seed_demo.py first (it creates Demo Big Mart and the demo customer).
You can run this script several times to get more packed orders.
Orders go to: Colony A, Colony A, Colony B, Colony C, Colony D.
To remove everything, run:  python seed_demo.py clear
"""
from db import get_connection
from localities import LOCALITIES

SELLER_EMAIL = "demo.bigmart@example.com"
CUSTOMER_EMAIL = "demo.customer@example.com"
DESTINATIONS = ["Colony A", "Colony A", "Colony B", "Colony C", "Colony D"]

conn = get_connection()
cursor = conn.cursor()
try:
    cursor.execute("SELECT seller_id FROM sellers WHERE email = %s", (SELLER_EMAIL,))
    seller = cursor.fetchone()
    cursor.execute("SELECT customer_id FROM customers WHERE email = %s", (CUSTOMER_EMAIL,))
    customer = cursor.fetchone()
    if not seller or not customer:
        print("Demo seller or customer not found. Run:  python seed_demo.py  first.")
        raise SystemExit(1)

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

    conn.commit()
    print("Done. Log in as demo.bigmart@example.com / demo123 and open 'Delivery groups'.")
except SystemExit:
    raise
except Exception:
    conn.rollback()
    raise
finally:
    cursor.close()
    conn.close()