"""
Creates demo data so that FVRS has something to rank.

    python seed_demo.py          -> removes old demo data, then creates fresh demo data
    python seed_demo.py clear    -> only removes the demo data

Demo sellers log in with their email and the password: demo123
All demo emails start with "demo." so they are easy to find and remove.
"""
import random
import sys
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from db import get_connection
from localities import LOCALITIES

DEMO_PASSWORD = "demo123"
DEMO_CUSTOMER_EMAIL = "demo.customer@example.com"

# shop name, email, colony, delivered orders in the last 7 days, cancelled orders, seller ratings
DEMO_SELLERS = [
    ("Demo Big Mart",     "demo.bigmart@example.com", "Colony A", 28, 2, [5, 4, 5, 4, 5, 4]),
    ("Demo Corner Shop",  "demo.corner@example.com",  "Colony B", 14, 1, [4, 4, 3, 5]),
    ("Demo Fresh Basket", "demo.fresh@example.com",   "Colony C", 7,  0, [5, 3]),
    ("Demo New Store",    "demo.newstore@example.com", "Colony D", 0,  0, []),
]

DEMO_PRODUCTS = [
    ("Demo Milk 500ml", "Dairy & Bakery", 30),
    ("Demo Bread", "Dairy & Bakery", 40),
    ("Demo Rice 1kg", "Groceries", 70),
]


def clear_demo(cursor):
    cursor.execute("SELECT seller_id FROM sellers WHERE email LIKE 'demo.%@example.com'")
    seller_ids = [row[0] for row in cursor.fetchall()]
    for sid in seller_ids:
        orders_of_seller = "SELECT order_id FROM orders WHERE seller_id = %s"
        products_of_seller = "SELECT product_id FROM products WHERE seller_id = %s"
        cursor.execute("DELETE FROM grouped_orders WHERE order_id IN (" + orders_of_seller + ")", (sid,))
        cursor.execute("DELETE FROM delivery_groups WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM complaints WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM seller_reviews WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM product_reviews WHERE product_id IN (" + products_of_seller + ")", (sid,))
        cursor.execute("DELETE FROM order_items WHERE order_id IN (" + orders_of_seller + ")", (sid,))
        cursor.execute("DELETE FROM orders WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM product_images WHERE product_id IN (" + products_of_seller + ")", (sid,))
        cursor.execute("DELETE FROM products WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM seller_metrics WHERE seller_id = %s", (sid,))
        cursor.execute("DELETE FROM sellers WHERE seller_id = %s", (sid,))
    return len(seller_ids)


def get_or_create_demo_customer(cursor):
    cursor.execute("SELECT customer_id FROM customers WHERE email = %s", (DEMO_CUSTOMER_EMAIL,))
    row = cursor.fetchone()
    if row:
        return row[0]
    lat, lon = LOCALITIES["Colony A"]
    cursor.execute(
        "INSERT INTO customers (full_name, email, phone, password_hash, address, latitude, longitude) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("Demo Customer", DEMO_CUSTOMER_EMAIL, "9999999999",
         generate_password_hash(DEMO_PASSWORD), "House 1, Colony A", lat, lon),
    )
    return cursor.lastrowid


def seed(cursor):
    random.seed(42)          # same random numbers every run
    now = datetime.now()
    customer_id = get_or_create_demo_customer(cursor)
    c_lat, c_lon = LOCALITIES["Colony A"]

    for shop, email, colony, delivered, cancelled, ratings in DEMO_SELLERS:
        lat, lon = LOCALITIES[colony]
        cursor.execute(
            "INSERT INTO sellers (shop_name, owner_name, email, phone, password_hash, "
            "shop_address, latitude, longitude, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'approved')",
            (shop, "Demo Owner", email, "8888888888", generate_password_hash(DEMO_PASSWORD),
             "Shop 1, " + colony, lat, lon),
        )
        seller_id = cursor.lastrowid

        products = []        # (product_id, price)
        for name, category, price in DEMO_PRODUCTS:
            cursor.execute(
                "INSERT INTO products (seller_id, product_name, category, description, price, stock_quantity) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (seller_id, name, category, "Demo product", price, 500),
            )
            products.append((cursor.lastrowid, price))

        def make_order(status, order_date, delivered_at):
            chosen = random.sample(products, k=random.randint(1, 2))
            lines = [(pid, random.randint(1, 3), price) for pid, price in chosen]
            total = sum(qty * price for _, qty, price in lines)
            cursor.execute(
                "INSERT INTO orders (customer_id, seller_id, total_amount, delivery_address, "
                "delivery_latitude, delivery_longitude, status, order_date, delivered_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (customer_id, seller_id, total, "House 1, Colony A", c_lat, c_lon,
                 status, order_date, delivered_at),
            )
            order_id = cursor.lastrowid
            for pid, qty, price in lines:
                cursor.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, unit_price) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, pid, qty, price),
                )
            return order_id

        delivered_ids = []
        for _ in range(delivered):
            delivered_at = now - timedelta(hours=random.randint(2, 7 * 24 - 2))
            delivered_ids.append(make_order("delivered", delivered_at - timedelta(hours=2), delivered_at))

        for _ in range(cancelled):
            order_date = now - timedelta(hours=random.randint(2, 7 * 24 - 2))
            make_order("cancelled", order_date, None)

        # Seller reviews attach to delivered orders (one review per order)
        for rating, order_id in zip(ratings, delivered_ids):
            cursor.execute(
                "INSERT INTO seller_reviews (customer_id, seller_id, order_id, rating, review_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                (customer_id, seller_id, order_id, rating, "Demo review"),
            )

        print("Created", shop, "-", delivered, "delivered,", cancelled, "cancelled,",
              len(ratings), "reviews")


conn = get_connection()
cursor = conn.cursor()
try:
    removed = clear_demo(cursor)
    print("Removed", removed, "old demo seller(s).")
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        print("Demo data cleared.")
    else:
        seed(cursor)
        print("Done. Demo seller login password:", DEMO_PASSWORD)
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    cursor.close()
    conn.close()