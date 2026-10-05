"""
Creates the admin account (run once).

Locally it uses the defaults below. On the hosted website, set the environment
variables ADMIN_EMAIL and ADMIN_PASSWORD first, so the admin password is private.
"""
import os

from werkzeug.security import generate_password_hash

from db import get_connection

ADMIN_NAME = os.environ.get("ADMIN_NAME", "Platform Admin")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@localmarket.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

conn = get_connection()
cursor = conn.cursor()

cursor.execute("SELECT admin_id FROM admins WHERE email = %s", (ADMIN_EMAIL,))
if cursor.fetchone():
    print("Admin already exists. Nothing to do.")
else:
    cursor.execute(
        "INSERT INTO admins (full_name, email, password_hash) VALUES (%s, %s, %s)",
        (ADMIN_NAME, ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD)),
    )
    conn.commit()
    print("Admin created:", ADMIN_EMAIL)

cursor.close()
conn.close()