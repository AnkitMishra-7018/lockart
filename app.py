import csv
import io
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, abort
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_connection, close_leftover_connections
from localities import LOCALITIES
from fvrs import (get_avg_orders_7d, compute_base_scores, save_daily_metrics,
                  rank_sellers_for_customer, haversine_km)
from grouping import group_orders, GROUP_RADIUS_KM, MAX_GROUP_SIZE

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")   # signs login cookies
IS_PRODUCTION = os.environ.get("APP_ENV") == "production"
app.config.update(
    SESSION_COOKIE_SECURE=IS_PRODUCTION,     # on the hosted site the cookie travels only over HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.teardown_appcontext(close_leftover_connections)   # always return database connections

# ---------- Contact details (read from environment variables, never typed into the code) ----------
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "").strip()
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()
_phone_digits = re.sub(r"\D", "", CONTACT_PHONE)
# 10-digit Indian numbers get the country code 91, so tel: and WhatsApp links work
CONTACT_PHONE_DIGITS = ("91" + _phone_digits) if len(_phone_digits) == 10 else _phone_digits

# ---------- Team shown on the About page: (name, role, initials, photo file in static/img/team) ----------
TEAM = [
    ("Ankit Mishra", "Team Leader", "AM", "ankit.jpg"),
    ("Amay Kr. Mishra", "Team Member", "AY", "amay.jpg"),
    ("Aman Pratap Singh", "Team Member", "AP", "aman_pratap.jpg"),
    ("Aman Kumar", "Team Member", "AK", "aman_kumar.jpg"),
]


# =====================================================
# SECURITY: CSRF PROTECTION AND LOGIN RATE-LIMITING
# =====================================================
# CSRF = a bad website tricking your browser into sending a form to Lockart while you are logged in.
# Defence: every form carries a secret token that only our own pages know, and every POST must send it back.
def get_csrf_token():
    """The secret token of this visitor's session (made the first time it is needed)."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


@app.before_request
def check_csrf_token():
    """Rejects any POST that does not carry this session's secret token."""
    if request.method != "POST":
        return None
    sent = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if expected and sent and secrets.compare_digest(sent.encode(), expected.encode()):
        return None
    return render_template("error.html", title="Session expired",
                           message="Your session has expired or the form was not valid. "
                                   "Please go back, refresh the page and try again."), 400


_FORM_TAG = re.compile(r"<form\b[^>]*>", re.I)
_POST_METHOD = re.compile(r"""method\s*=\s*["']?post""", re.I)


@app.after_request
def add_csrf_fields(response):
    """Adds the hidden token field inside every POST form of every page, so no template needs editing."""
    if response.mimetype != "text/html" or response.direct_passthrough:
        return response
    html = response.get_data(as_text=True)
    if "<form" not in html.lower():
        return response
    field = '<input type="hidden" name="csrf_token" value="' + get_csrf_token() + '">'
    html = _FORM_TAG.sub(lambda m: m.group(0) + field if _POST_METHOD.search(m.group(0)) else m.group(0), html)
    response.set_data(html)
    return response


# After 5 wrong passwords for the same account, logging in is blocked for 10 minutes.
# The attempts are kept in the database, so all of the website's workers share the same count.
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_MINUTES = 10
LOGIN_LOCK_MESSAGE = ("Too many wrong attempts. For safety, login is blocked for "
                      + str(LOGIN_WINDOW_MINUTES) + " minutes. Please try again later.")


def login_key(role, email):
    return (role + ":" + email)[:150]


def login_blocked(role, email):
    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM login_attempts WHERE attempt_key = %s "
                   "AND attempted_at >= NOW() - INTERVAL %s MINUTE",
                   (login_key(role, email), LOGIN_WINDOW_MINUTES))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return count >= LOGIN_MAX_FAILS


def record_failed_login(role, email):
    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO login_attempts (attempt_key) VALUES (%s)", (login_key(role, email),))
    cursor.execute("DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL 1 DAY")   # tidy up
    conn.commit()
    cursor.close()
    conn.close()


def clear_failed_logins(role, email):
    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM login_attempts WHERE attempt_key = %s", (login_key(role, email),))
    conn.commit()
    cursor.close()
    conn.close()

# ---------- Settings for product image uploads ----------
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024      # max 2 MB per upload
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)  # creates static/uploads automatically

SHOW_FVRS_ON_CARDS = True      # show each shop's score on product cards (for demos; set False for a "real" look)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
CATEGORIES = ["Groceries", "Dairy & Bakery", "Fruits & Vegetables",
              "Snacks & Beverages", "Household", "Personal Care",
              "Stationery", "Other"]


@app.errorhandler(413)
def file_too_large(error):
    flash("That file is too large. Maximum size is 2 MB.", "error")
    return redirect(url_for("seller_products"))


# =====================================================
# IMAGES STORED INSIDE THE DATABASE
# (the hosting website's own folders are wiped on every restart,
#  so seller-uploaded photos live in the image_store table instead)
# =====================================================
@app.route("/media/<int:image_id>")
def media(image_id):
    """Sends one stored image to the browser."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT mime_type, image_data FROM image_store WHERE image_id = %s", (image_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        abort(404)
    response = Response(bytes(row[1]), mimetype=row[0])
    # An image's id never changes (a new upload gets a new id), so browsers may keep it for 30 days.
    response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return response


@app.template_global()
def img_url(path):
    """Turns a stored image path into a web address. Templates call this instead of url_for('static', ...).
    'db/12'  -> /media/12 (photo kept in the database)
    'img/..' -> static file in git (demo photos)
    Old 'uploads/..' files that Render already deleted -> a 'No image' picture."""
    if not path:
        return url_for("static", filename="img/no-image.svg")
    if path.startswith("db/"):
        try:
            return url_for("media", image_id=int(path[3:]))
        except ValueError:
            return url_for("static", filename="img/no-image.svg")
    if not os.path.exists(os.path.join(app.root_path, "static", path)):
        return url_for("static", filename="img/no-image.svg")
    return url_for("static", filename=path)


# =====================================================
# GENERAL PAGES
# =====================================================
CATEGORY_TILES = [("Groceries", "bigmart_rice.jpg"), ("Dairy & Bakery", "corner_milk.jpg"),
                  ("Fruits & Vegetables", "fresh_apples.jpg"), ("Snacks & Beverages", "newstore_peanuts.jpg"),
                  ("Household", "newstore_dishwash.jpg"), ("Personal Care", "newstore_soap.jpg"),
                  ("Stationery", "newstore_notebook.jpg")]
SHOP_COVERS = {"big mart": "cover_bigmart.jpg", "corner": "cover_corner.jpg",
               "fresh basket": "cover_fresh.jpg", "new store": "cover_newstore.jpg"}

# Same columns as the search page, so the product card works for both
PRODUCT_SELECT = (
    "SELECT p.product_id, p.product_name, p.category, p.price, p.stock_quantity, s.shop_name, "
    "(SELECT image_path FROM product_images WHERE product_id = p.product_id LIMIT 1), "
    "(SELECT ROUND(AVG(rating), 1) FROM product_reviews WHERE product_id = p.product_id), "
    "(SELECT COUNT(*) FROM product_reviews WHERE product_id = p.product_id), s.seller_id "
    "FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
    "WHERE p.is_active = 1 AND s.status = 'approved' AND p.stock_quantity > 0 "
)


@app.route("/")
def home():
    """Home page: a few products, and more of everything else (slider, shops, explainers)."""
    conn = get_connection()
    cursor = conn.cursor()
    customer_lat = customer_lon = None
    if "customer_id" in session:
        cursor.execute("SELECT latitude, longitude FROM customers WHERE customer_id = %s",
                       (session["customer_id"],))
        loc = cursor.fetchone()
        if loc and loc[0] is not None and loc[1] is not None:
            customer_lat, customer_lon = float(loc[0]), float(loc[1])
    ranking = rank_sellers_for_customer(cursor, customer_lat, customer_lon)
    scores = {r["seller_id"]: r for r in ranking}

    cursor.execute(PRODUCT_SELECT + "ORDER BY p.created_at DESC LIMIT 200")
    products = cursor.fetchall()

    reorder = []
    if "customer_id" in session:
        cursor.execute(
            PRODUCT_SELECT + "AND p.product_id IN (SELECT oi.product_id FROM order_items oi "
            "JOIN orders o ON o.order_id = oi.order_id WHERE o.customer_id = %s) "
            "ORDER BY p.created_at DESC LIMIT 4", (session["customer_id"],))
        reorder = cursor.fetchall()
    cursor.close()
    conn.close()

    # Picks: up to 2 products from each shop, best-ranked shops first, 8 in total
    by_shop = {}
    for p in products:
        by_shop.setdefault(p[9], []).append(p)
    ranked_ids = [r["seller_id"] for r in ranking if r["seller_id"] in by_shop]
    picks = []
    for round_no in range(2):
        for sid in ranked_ids:
            if len(by_shop[sid]) > round_no:
                picks.append(by_shop[sid][round_no])
    new_ids = [r["seller_id"] for r in ranking if r["is_new"]]
    from_new = [p for p in products if p[9] in new_ids][:4]

    shops = ranking[:4]
    for r in shops:
        name = r["shop_name"].lower()
        r["cover"] = next(("img/covers/" + f for k, f in SHOP_COVERS.items() if k in name), None)

    return render_template("home.html", picks=picks[:8], from_new=from_new, reorder=reorder,
                           shops=shops, scores=scores, show_scores=SHOW_FVRS_ON_CARDS,
                           categories=CATEGORY_TILES)


PER_PAGE = 12
SORT_OPTIONS = [("best", "Best match (fair ranking)"), ("price_asc", "Price: low to high"),
                ("price_desc", "Price: high to low"), ("newest", "Newest"), ("rating", "Top rated")]


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    category = category if category in CATEGORIES else ""
    sort = request.args.get("sort", "best")
    in_stock = request.args.get("in_stock") == "1"

    def number(name):
        try:
            return float(request.args.get(name, ""))
        except ValueError:
            return None

    min_price, max_price, min_rating = number("min_price"), number("max_price"), number("min_rating")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    conditions, params = [], []
    if q:
        conditions.append("(p.product_name LIKE %s OR s.shop_name LIKE %s)")
        params += ["%" + q + "%", "%" + q + "%"]
    if category:
        conditions.append("p.category = %s")
        params.append(category)
    if min_price is not None:
        conditions.append("p.price >= %s")
        params.append(min_price)
    if max_price is not None:
        conditions.append("p.price <= %s")
        params.append(max_price)
    if in_stock:
        conditions.append("p.stock_quantity > 0")
    sql = (PRODUCT_SELECT.replace("AND p.stock_quantity > 0 ", "")
           + "".join(" AND " + c for c in conditions) + " ORDER BY p.created_at DESC LIMIT 500")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    products = cursor.fetchall()
    customer_lat = customer_lon = None
    if "customer_id" in session:
        cursor.execute("SELECT latitude, longitude FROM customers WHERE customer_id = %s",
                       (session["customer_id"],))
        loc = cursor.fetchone()
        if loc and loc[0] is not None and loc[1] is not None:
            customer_lat, customer_lon = float(loc[0]), float(loc[1])
    scores = {r["seller_id"]: r for r in rank_sellers_for_customer(cursor, customer_lat, customer_lon)}
    cursor.close()
    conn.close()

    if min_rating is not None:
        products = [p for p in products if p[7] is not None and p[7] >= min_rating]
    if sort == "price_asc":
        products.sort(key=lambda p: p[3])
    elif sort == "price_desc":
        products.sort(key=lambda p: -p[3])
    elif sort == "rating":
        products.sort(key=lambda p: (-(float(p[7]) if p[7] is not None else 0), -p[8]))
    elif sort != "newest":
        sort = "best"            # fair ranking: in stock first, then the shop's FVRS score
        products.sort(key=lambda p: (p[4] <= 0, -scores.get(p[9], {}).get("final_score", 0)))

    total = len(products)
    pages = max(1, -(-total // PER_PAGE))
    page = min(page, pages)
    items = products[(page - 1) * PER_PAGE: page * PER_PAGE]

    raw = {"q": q, "category": category, "min_price": request.args.get("min_price", "").strip(),
           "max_price": request.args.get("max_price", "").strip(),
           "min_rating": request.args.get("min_rating", "").strip(),
           "in_stock": "1" if in_stock else "", "sort": sort if sort != "best" else ""}
    args = {k: v for k, v in raw.items() if v}
    labels = {"q": '"' + q + '"', "category": category, "min_price": "From Rs " + raw["min_price"],
              "max_price": "Up to Rs " + raw["max_price"], "min_rating": raw["min_rating"] + " stars and up",
              "in_stock": "In stock only"}
    chips = [(labels[k], url_for("search", **{x: y for x, y in args.items() if x != k}))
             for k in labels if k in args]
    page_links = [(n, url_for("search", **args, page=n)) for n in range(1, pages + 1)]

    return render_template("search.html", products=items, total=total, q=q, args=args, chips=chips,
                           categories=CATEGORIES, sort=sort if sort else "best", sort_options=SORT_OPTIONS,
                           page=page, pages=pages, page_links=page_links, scores=scores,
                           show_scores=SHOW_FVRS_ON_CARDS)


@app.route("/shops")
def shops():
    """Shows how FVRS ranks the shops for the current visitor (good for demos and the viva)."""
    customer_lat = customer_lon = None
    conn = get_connection()
    cursor = conn.cursor()
    if "customer_id" in session:
        cursor.execute("SELECT latitude, longitude FROM customers WHERE customer_id = %s",
                       (session["customer_id"],))
        location = cursor.fetchone()
        if location and location[0] is not None and location[1] is not None:
            customer_lat, customer_lon = float(location[0]), float(location[1])
    ranking = rank_sellers_for_customer(cursor, customer_lat, customer_lon)
    cursor.close()
    conn.close()
    return render_template("shops.html", ranking=ranking,
                           has_location=customer_lat is not None)


_wishlist_ready = False


def ensure_wishlist():
    """Creates the wishlist table the first time it is needed (works on every database)."""
    global _wishlist_ready
    if _wishlist_ready:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS wishlist (customer_id INT NOT NULL, product_id INT NOT NULL, "
        "added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (customer_id, product_id), "
        "FOREIGN KEY (customer_id) REFERENCES customers(customer_id), "
        "FOREIGN KEY (product_id) REFERENCES products(product_id))")
    conn.commit()
    cursor.close()
    conn.close()
    _wishlist_ready = True


def cover_for(shop_name):
    name = shop_name.lower()
    return next(("img/covers/" + f for k, f in SHOP_COVERS.items() if k in name), None)


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT p.product_id, p.product_name, p.category, p.description, p.price, "
        "p.stock_quantity, s.seller_id, s.shop_name, s.shop_address "
        "FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
        "WHERE p.product_id = %s AND p.is_active = 1 AND s.status = 'approved'", (product_id,))
    product = cursor.fetchone()
    if not product:
        cursor.close()
        conn.close()
        flash("That product is not available.", "error")
        return redirect(url_for("home"))

    cursor.execute("SELECT image_path FROM product_images WHERE product_id = %s", (product_id,))
    images = [row["image_path"] for row in cursor.fetchall()]
    cursor.execute("SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS review_count "
                   "FROM product_reviews WHERE product_id = %s", (product_id,))
    product_rating = cursor.fetchone()
    cursor.execute("SELECT rating, COUNT(*) AS n FROM product_reviews WHERE product_id = %s GROUP BY rating",
                   (product_id,))
    counts = {r["rating"]: r["n"] for r in cursor.fetchall()}
    total = sum(counts.values())
    bars = [(star, counts.get(star, 0), round(counts.get(star, 0) * 100 / total) if total else 0)
            for star in (5, 4, 3, 2, 1)]
    cursor.execute("SELECT r.rating, r.review_text, r.review_date, c.full_name FROM product_reviews r "
                   "JOIN customers c ON c.customer_id = r.customer_id WHERE r.product_id = %s "
                   "ORDER BY r.review_date DESC", (product_id,))
    reviews = cursor.fetchall()
    cursor.execute("SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS review_count "
                   "FROM seller_reviews WHERE seller_id = %s", (product["seller_id"],))
    seller_rating = cursor.fetchone()

    wished = False
    if "customer_id" in session:
        ensure_wishlist()
        cursor.execute("SELECT 1 AS x FROM wishlist WHERE customer_id = %s AND product_id = %s",
                       (session["customer_id"], product_id))
        wished = cursor.fetchone() is not None
    plain = conn.cursor()
    plain.execute(PRODUCT_SELECT + "AND p.seller_id = %s AND p.product_id <> %s "
                  "ORDER BY p.created_at DESC LIMIT 4", (product["seller_id"], product_id))
    more = plain.fetchall()
    plain.close()
    cursor.close()
    conn.close()
    return render_template("product_detail.html", product=product, images=images,
                           product_rating=product_rating, bars=bars, reviews=reviews,
                           seller_rating=seller_rating, wished=wished, more=more)


@app.route("/shop/<int:seller_id>")
def shop_page(seller_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT seller_id, shop_name, shop_address FROM sellers "
                   "WHERE seller_id = %s AND status = 'approved'", (seller_id,))
    shop = cursor.fetchone()
    if not shop:
        cursor.close()
        conn.close()
        flash("That shop is not available.", "error")
        return redirect(url_for("shops"))
    cursor.execute("SELECT r.rating, r.review_text, c.full_name FROM seller_reviews r "
                   "JOIN customers c ON c.customer_id = r.customer_id WHERE r.seller_id = %s "
                   "ORDER BY r.review_date DESC LIMIT 6", (seller_id,))
    reviews = cursor.fetchall()
    plain = conn.cursor()
    lat = lon = None
    if "customer_id" in session:
        plain.execute("SELECT latitude, longitude FROM customers WHERE customer_id = %s",
                      (session["customer_id"],))
        loc = plain.fetchone()
        if loc and loc[0] is not None and loc[1] is not None:
            lat, lon = float(loc[0]), float(loc[1])
    info = next((r for r in rank_sellers_for_customer(plain, lat, lon) if r["seller_id"] == seller_id), None)
    plain.execute(PRODUCT_SELECT + "AND p.seller_id = %s ORDER BY p.created_at DESC", (seller_id,))
    products = plain.fetchall()
    plain.close()
    cursor.close()
    conn.close()
    return render_template("shop.html", shop=shop, info=info, products=products, reviews=reviews,
                           cover=cover_for(shop["shop_name"]))


@app.route("/wishlist")
def wishlist():
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))
    ensure_wishlist()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(PRODUCT_SELECT + "AND p.product_id IN (SELECT product_id FROM wishlist "
                   "WHERE customer_id = %s) ORDER BY p.created_at DESC", (session["customer_id"],))
    products = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("wishlist.html", products=products)


@app.route("/wishlist/toggle/<int:product_id>", methods=["POST"])
def wishlist_toggle(product_id):
    if not customer_logged_in():
        flash("Please log in as a customer to save products.", "error")
        return redirect(url_for("login"))
    ensure_wishlist()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM wishlist WHERE customer_id = %s AND product_id = %s",
                   (session["customer_id"], product_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO wishlist (customer_id, product_id) VALUES (%s, %s)",
                       (session["customer_id"], product_id))
        flash("Saved to your wishlist.", "success")
    else:
        flash("Removed from your wishlist.", "success")
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(request.referrer or url_for("product_detail", product_id=product_id))


@app.route("/about")
def about():
    team = [{"name": n, "role": r, "initials": i, "photo": p} for n, r, i, p in TEAM]
    # Photos are shown only when all four exist, so the page never mixes photos and initials
    team_folder = os.path.join(app.root_path, "static", "img", "team")
    show_photos = all(os.path.exists(os.path.join(team_folder, m["photo"])) for m in team)
    return render_template("about.html", team=team, show_photos=show_photos)


# =====================================================
# INFO PAGES: contact form, newsletter, FAQ, policies, order tracking
# =====================================================
_extra_tables_ready = False


def ensure_extra_tables():
    """Creates the contact_messages and newsletter_subscribers tables the first time they are needed
    (same idea as ensure_wishlist). schema.sql also has them, for a fresh database."""
    global _extra_tables_ready
    if _extra_tables_ready:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS contact_messages (message_id INT PRIMARY KEY AUTO_INCREMENT, "
        "full_name VARCHAR(100) NOT NULL, email VARCHAR(100) NOT NULL, message_text TEXT NOT NULL, "
        "status ENUM('new','read') NOT NULL DEFAULT 'new', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS newsletter_subscribers (subscriber_id INT PRIMARY KEY AUTO_INCREMENT, "
        "email VARCHAR(100) NOT NULL UNIQUE, subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS login_attempts (attempt_id INT PRIMARY KEY AUTO_INCREMENT, "
        "attempt_key VARCHAR(150) NOT NULL, attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_attempt (attempt_key, attempted_at))")
    conn.commit()
    cursor.close()
    conn.close()
    _extra_tables_ready = True


def valid_email(email):
    """A simple check: something@something.something, no spaces, at most 100 characters."""
    return len(email) <= 100 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def password_problem(password):
    """Returns a message if the password is too weak, otherwise None.
    Rule: 8 to 128 characters, with at least one letter and one number."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    if not re.search(r"[A-Za-z]", password):
        return "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one number."
    return None


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form = {"full_name": session.get("customer_name", ""), "email": "", "message": ""}
    if request.method == "POST":
        form = {key: request.form.get(key, "").strip() for key in ("full_name", "email", "message")}
        if request.form.get("website"):
            # A hidden field that real visitors never see: only spam bots fill it in. Pretend it worked.
            flash("Thank you! Your message has been sent.", "success")
            return redirect(url_for("contact"))
        if not form["full_name"] or len(form["full_name"]) > 100:
            flash("Please enter your name (up to 100 characters).", "error")
        elif not valid_email(form["email"]):
            flash("Please enter a valid email address.", "error")
        elif len(form["message"]) < 10:
            flash("Please write a message of at least 10 characters.", "error")
        elif len(form["message"]) > 1000:
            flash("Your message is too long. Please keep it under 1000 characters.", "error")
        else:
            ensure_extra_tables()
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO contact_messages (full_name, email, message_text) VALUES (%s, %s, %s)",
                           (form["full_name"], form["email"], form["message"]))
            conn.commit()
            cursor.close()
            conn.close()
            flash("Thank you! Your message has been sent. We will get back to you by email.", "success")
            return redirect(url_for("contact"))
    return render_template("contact.html", form=form)


@app.route("/newsletter", methods=["POST"])
def newsletter():
    email = request.form.get("email", "").strip().lower()
    if not valid_email(email):
        flash("Please enter a valid email address to subscribe.", "error")
    else:
        ensure_extra_tables()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT IGNORE INTO newsletter_subscribers (email) VALUES (%s)", (email,))
        conn.commit()
        cursor.close()
        conn.close()
        flash("Thanks for subscribing! We will keep you updated.", "success")
    return redirect(request.referrer or url_for("home"))


FAQS = [
    ("What is Lockart?",
     "Lockart (Local Kart) is an online marketplace for neighbourhood shops. It is a college mini project, "
     "so it is a working prototype and not a real store."),
    ("Do I pay online?",
     "No. Every order is cash on delivery. There is no payment gateway and no card or bank details are asked."),
    ("Can I order from more than one shop at once?",
     "Yes. Your cart can hold items from several shops, but each shop gets its own order and delivers separately."),
    ("How do I cancel an order?",
     "Open My Orders and press Cancel. You can cancel only while the order is still pending, "
     "before the shop has accepted it. The stock goes back to the shop."),
    ("How do I know where my order is?",
     "Open My Orders for the live status, or use Track your order with your order number and the email "
     "you registered with."),
    ("Why do new shops show up near the top?",
     "Lockart uses a Fair Visibility Ranking System (FVRS). Shops with fewer recent orders get more visibility, "
     "and a new shop starts with a fair boost, so small shops are not buried by busy ones."),
    ("What does the \"New shop\" badge mean?",
     "The shop has no completed orders and no reviews yet. It is brand new on Lockart."),
    ("What if my order is late, wrong or damaged?",
     "After the order is placed, open My Orders and raise a complaint for that order. The admin reviews it "
     "and marks it resolved or rejected."),
    ("When can I write a review?",
     "Only after your order has been delivered. You can review the shop once per order and each product once."),
    ("How do I sell on Lockart?",
     "Press Sell with us and register your shop. The admin approves new shops before they appear on the site."),
    ("What is smart delivery grouping?",
     "A prototype feature. A seller's packed orders that are going to places within 1 km of each other are "
     "grouped into one delivery trip. It is a simple method, not a real route planner."),
    ("Are the product photos real?",
     "The demo photos are AI-generated for demonstration. Photos uploaded by sellers are shown as uploaded."),
]


@app.route("/faq")
def faq():
    return render_template("faq.html", faqs=FAQS)


POLICY_PAGES = {
    "terms": ("Terms of use", [
        ("About this site",
         ["Lockart is a college mini project built by a student team. It is a prototype for learning and "
          "demonstration, not a real store. No real payment or delivery takes place, so please use fake details."]),
        ("Accounts",
         ["You are responsible for keeping your password private. Please give correct information when you "
          "register. Passwords are stored in a scrambled (hashed) form."]),
        ("Orders",
         ["Placing an order sends a request to the shop. The shop may accept or cancel it. All orders are "
          "cash on delivery. A customer can cancel only while the order is pending."]),
        ("Sellers",
         ["Shops are approved by the admin before they appear. The admin can block a shop that breaks the rules "
          "or receives serious complaints. Sellers must describe their products honestly."]),
        ("Reviews and complaints",
         ["Reviews can be written only after delivery. Please be honest and respectful. Complaints are "
          "reviewed by the admin."]),
        ("Changes",
         ["These terms may change as the project develops."]),
    ]),
    "privacy": ("Privacy policy", [
        ("What we store",
         ["Your name, email, phone number, address and colony, your orders, reviews, complaints and wishlist. "
          "If you write to us or subscribe to updates, we also store the message or email address you give."]),
        ("Why we store it",
         ["To run the site: to show you shops near you, deliver your orders and let you review them."]),
        ("What we do not do",
         ["We do not take payment details, because every order is cash on delivery. We do not sell your "
          "information. Your phone number and address are shown only to the shop that is delivering your order."]),
        ("Cookies",
         ["Lockart uses one session cookie to keep you logged in and to remember your cart. We do not use "
          "advertising or tracking cookies."]),
        ("Your data",
         ["You can change your name, phone, address and password on your Profile page. To ask for your data "
          "to be removed, use the Contact page."]),
        ("A reminder",
         ["This is a student project. Please do not enter real sensitive information."]),
    ]),
    "shipping": ("Shipping and delivery", [
        ("Who delivers",
         ["Each shop delivers its own orders. If you order from three shops, you get three separate deliveries."]),
        ("Where",
         ["Delivery is available in the colonies listed at checkout. You can use your registered address or "
          "choose another colony and add your house details."]),
        ("How long",
         ["The time depends on the shop. You can follow the status in My Orders: pending, accepted, packed, "
          "out for delivery and delivered."]),
        ("Grouped trips",
         ["A shop may deliver nearby orders together in one trip. This is a prototype feature and does not "
          "promise a delivery time."]),
        ("Paying",
         ["Pay in cash when the order arrives. The total shown at checkout is what you pay."]),
    ]),
    "returns": ("Returns and refunds", [
        ("Cancelling",
         ["You can cancel an order yourself while it is still pending. After the shop accepts it, "
          "please contact the shop or raise a complaint."]),
        ("Wrong, damaged or late orders",
         ["Open My Orders and raise a complaint for that order, choosing the type of problem. The admin reviews "
          "it and marks it resolved or rejected. The shop can see the complaint."]),
        ("Refunds",
         ["Because every order is cash on delivery, no money is paid online, so there is nothing to refund "
          "online. Any money-back or replacement is agreed with the shop."]),
        ("A reminder",
         ["Lockart is a college prototype, so no real money or goods change hands."]),
    ]),
}


@app.route("/<page>")
def policy_page(page):
    if page not in POLICY_PAGES:
        abort(404)
    title, sections = POLICY_PAGES[page]
    return render_template("policy.html", title=title, sections=sections)


TRACK_STEPS = [("pending", "Placed"), ("accepted", "Accepted"), ("packed", "Packed"),
               ("out_for_delivery", "Out for delivery"), ("delivered", "Delivered")]


@app.route("/track-order", methods=["GET", "POST"])
def track_order():
    order = None
    email = ""
    if request.method == "POST":
        order_number = request.form.get("order_id", "").strip().lstrip("#")
        email = request.form.get("email", "").strip().lower()
        if not order_number.isdigit() or len(order_number) > 9 or not valid_email(email):
            flash("Please enter your order number (digits only) and the email you registered with.", "error")
        else:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            # The order number alone is not enough: the email must match the customer who placed the order.
            cursor.execute(
                "SELECT o.order_id, o.status, o.order_date, o.total_amount, o.delivered_at, s.shop_name "
                "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                "JOIN sellers s ON s.seller_id = o.seller_id "
                "WHERE o.order_id = %s AND LOWER(c.email) = %s",
                (int(order_number), email))
            order = cursor.fetchone()
            if order:
                cursor.execute(
                    "SELECT p.product_name, oi.quantity, oi.unit_price FROM order_items oi "
                    "JOIN products p ON p.product_id = oi.product_id WHERE oi.order_id = %s",
                    (order["order_id"],))
                order["lines"] = cursor.fetchall()
                if order["status"] != "cancelled":
                    position = [key for key, _ in TRACK_STEPS].index(order["status"])
                    order["steps"] = [(label, index <= position) for index, (_, label) in enumerate(TRACK_STEPS)]
            else:
                # Same message whether the order does not exist or belongs to someone else.
                flash("We could not find an order with those details. Please check and try again.", "error")
            cursor.close()
            conn.close()
    return render_template("track_order.html", order=order, email=email)


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))
    cid = session["customer_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT full_name, email, phone, address, password_hash FROM customers "
                   "WHERE customer_id = %s", (cid,))
    customer = cursor.fetchone()

    if request.method == "POST":
        if request.form.get("action") == "password":
            new = request.form.get("new", "")
            problem = password_problem(new)
            if not check_password_hash(customer["password_hash"], request.form.get("current", "")):
                flash("Your current password is not correct.", "error")
            elif problem:
                flash("The new password is too weak. " + problem, "error")
            else:
                cursor.execute("UPDATE customers SET password_hash = %s WHERE customer_id = %s",
                               (generate_password_hash(new), cid))
                conn.commit()
                flash("Password changed.", "success")
        else:
            name = request.form.get("full_name", "").strip()
            phone = request.form.get("phone", "").strip()
            locality = request.form.get("locality", "")
            house = request.form.get("house", "").strip()
            if not name or not phone:
                flash("Name and phone cannot be empty.", "error")
            elif locality and (locality not in LOCALITIES or not house):
                flash("Please add your house/flat details for the chosen colony.", "error")
            elif locality:
                lat, lon = LOCALITIES[locality]
                cursor.execute("UPDATE customers SET full_name=%s, phone=%s, address=%s, latitude=%s, "
                               "longitude=%s WHERE customer_id=%s",
                               (name, phone, house + ", " + locality, lat, lon, cid))
                conn.commit()
                session["customer_name"] = name
                flash("Profile updated.", "success")
            else:
                cursor.execute("UPDATE customers SET full_name=%s, phone=%s WHERE customer_id=%s",
                               (name, phone, cid))
                conn.commit()
                session["customer_name"] = name
                flash("Profile updated.", "success")
        cursor.close()
        conn.close()
        return redirect(url_for("profile"))

    cursor.close()
    conn.close()
    return render_template("profile.html", customer=customer, localities=LOCALITIES.keys())


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


# =====================================================
# CUSTOMER: REGISTER AND LOGIN
# =====================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        password = request.form["password"]
        locality = request.form["locality"]
        house = request.form["address"].strip()

        problem = password_problem(password)
        if problem:
            flash(problem, "error")
            return render_template("register.html", localities=LOCALITIES.keys())
        if locality not in LOCALITIES:
            flash("Please choose a valid locality.", "error")
            return render_template("register.html", localities=LOCALITIES.keys())

        latitude, longitude = LOCALITIES[locality]
        full_address = house + ", " + locality
        hashed = generate_password_hash(password)

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT customer_id FROM customers WHERE email = %s", (email,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            flash("This email is already registered.", "error")
            return render_template("register.html", localities=LOCALITIES.keys())

        cursor.execute(
            "INSERT INTO customers (full_name, email, phone, password_hash, address, latitude, longitude) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (full_name, email, phone, hashed, full_address, latitude, longitude),
        )
        conn.commit()
        cursor.close()
        conn.close()

        flash("Registration successful! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", localities=LOCALITIES.keys())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if login_blocked("customer", email):
            flash(LOGIN_LOCK_MESSAGE, "error")
            return render_template("login.html")

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT customer_id, full_name, password_hash FROM customers WHERE email = %s",
            (email,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and check_password_hash(row[2], password):
            clear_failed_logins("customer", email)
            session.clear()                      # forget any earlier login (customer, seller or admin)
            session["customer_id"] = row[0]
            session["customer_name"] = row[1]
            flash("Logged in successfully.", "success")
            return redirect(url_for("home"))

        record_failed_login("customer", email)
        flash("Invalid email or password.", "error")

    return render_template("login.html")


# =====================================================
# SELLER: REGISTER, LOGIN, DASHBOARD
# =====================================================
@app.route("/seller/register", methods=["GET", "POST"])
def seller_register():
    if request.method == "POST":
        shop_name = request.form["shop_name"].strip()
        owner_name = request.form["owner_name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        password = request.form["password"]
        locality = request.form["locality"]
        shop_details = request.form["shop_address"].strip()

        problem = password_problem(password)
        if problem:
            flash(problem, "error")
            return render_template("seller_register.html", localities=LOCALITIES.keys())
        if locality not in LOCALITIES:
            flash("Please choose a valid locality.", "error")
            return render_template("seller_register.html", localities=LOCALITIES.keys())

        latitude, longitude = LOCALITIES[locality]
        full_address = shop_details + ", " + locality
        hashed = generate_password_hash(password)

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT seller_id FROM sellers WHERE email = %s", (email,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            flash("This email is already registered.", "error")
            return render_template("seller_register.html", localities=LOCALITIES.keys())

        # status is not listed here, so MySQL uses the default: 'pending'
        cursor.execute(
            "INSERT INTO sellers (shop_name, owner_name, email, phone, password_hash, "
            "shop_address, latitude, longitude) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (shop_name, owner_name, email, phone, hashed, full_address, latitude, longitude),
        )
        conn.commit()
        cursor.close()
        conn.close()

        flash("Shop registered! You can log in once an admin approves your shop.", "success")
        return redirect(url_for("seller_login"))

    return render_template("seller_register.html", localities=LOCALITIES.keys())


@app.route("/seller/login", methods=["GET", "POST"])
def seller_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if login_blocked("seller", email):
            flash(LOGIN_LOCK_MESSAGE, "error")
            return render_template("seller_login.html")

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT seller_id, shop_name, password_hash, status FROM sellers WHERE email = %s",
            (email,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        # Password is checked first, so strangers can't learn a shop's status
        if not row or not check_password_hash(row[2], password):
            record_failed_login("seller", email)
            flash("Invalid email or password.", "error")
            return render_template("seller_login.html")
        clear_failed_logins("seller", email)

        status = row[3]
        if status == "pending":
            flash("Your shop is waiting for admin approval.", "error")
            return render_template("seller_login.html")
        if status == "blocked":
            flash("Your shop has been blocked. Please contact the admin.", "error")
            return render_template("seller_login.html")

        session.clear()                          # forget any earlier login
        session["seller_id"] = row[0]
        session["seller_shop"] = row[1]
        flash("Logged in successfully.", "success")
        return redirect(url_for("seller_dashboard"))

    return render_template("seller_login.html")


@app.route("/seller/dashboard")
def seller_dashboard():
    if "seller_id" not in session:
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))
    seller_id = session["seller_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # One query counts orders by status and adds up delivered sales
    cursor.execute(
        "SELECT COALESCE(SUM(status NOT IN ('delivered', 'cancelled')), 0) AS current_orders, "
        "COALESCE(SUM(status = 'delivered'), 0) AS delivered_orders, "
        "COALESCE(SUM(status = 'cancelled'), 0) AS cancelled_orders, "
        "COALESCE(SUM(CASE WHEN status = 'delivered' THEN total_amount END), 0) AS sales_total "
        "FROM orders WHERE seller_id = %s",
        (seller_id,),
    )
    order_stats = cursor.fetchone()

    cursor.execute(
        "SELECT COUNT(*) AS product_count FROM products WHERE seller_id = %s AND is_active = 1",
        (seller_id,),
    )
    product_count = cursor.fetchone()["product_count"]

    cursor.execute(
        "SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS review_count "
        "FROM seller_reviews WHERE seller_id = %s",
        (seller_id,),
    )
    rating = cursor.fetchone()

    cursor.execute(
        "SELECT COUNT(*) AS pending_complaints FROM complaints "
        "WHERE seller_id = %s AND status = 'pending'",
        (seller_id,),
    )
    pending_complaints = cursor.fetchone()["pending_complaints"]

    cursor.execute(
        "SELECT product_name, stock_quantity FROM products "
        "WHERE seller_id = %s AND is_active = 1 AND stock_quantity <= 5 "
        "ORDER BY stock_quantity",
        (seller_id,),
    )
    low_stock = cursor.fetchall()

    # Delivered orders and sales for each of the last 7 days (Indian dates), zero if none
    cursor.execute(
        "SELECT DATE(delivered_at) AS d, COUNT(*) AS n, COALESCE(SUM(total_amount), 0) AS sales "
        "FROM orders WHERE seller_id = %s AND status = 'delivered' "
        "AND delivered_at >= CURDATE() - INTERVAL 6 DAY GROUP BY DATE(delivered_at)", (seller_id,))
    found = {row["d"]: row for row in cursor.fetchall()}
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    daily = []
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        row = found.get(day)
        daily.append({"label": day.strftime("%a"), "orders": int(row["n"]) if row else 0,
                      "sales": float(row["sales"]) if row else 0.0})

    # FVRS piece 1: the 7-day average (the helper expects a normal cursor)
    plain_cursor = conn.cursor()
    ranking = compute_base_scores(plain_cursor)
    plain_cursor.close()
    my_fvrs = next((r for r in ranking if r["seller_id"] == seller_id), None)
    my_rank = ranking.index(my_fvrs) + 1 if my_fvrs else None
    avg_orders_7d = my_fvrs["avg_orders"] if my_fvrs else 0

    cursor.close()
    conn.close()
    return render_template("seller_dashboard.html", shop_name=session["seller_shop"],
                           order_stats=order_stats, product_count=product_count,
                           rating=rating, pending_complaints=pending_complaints,
                           low_stock=low_stock, avg_orders_7d=avg_orders_7d,
                           my_fvrs=my_fvrs, my_rank=my_rank, shop_count=len(ranking), daily=daily)


# =====================================================
# ADMIN: LOGIN, DASHBOARD, APPROVE / BLOCK SELLERS
# =====================================================
def admin_logged_in():
    return "admin_id" in session


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if login_blocked("admin", email):
            flash(LOGIN_LOCK_MESSAGE, "error")
            return render_template("admin_login.html")

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT admin_id, full_name, password_hash FROM admins WHERE email = %s",
            (email,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and check_password_hash(row[2], password):
            clear_failed_logins("admin", email)
            session.clear()                      # forget any earlier login
            session["admin_id"] = row[0]
            session["admin_name"] = row[1]
            flash("Admin logged in.", "success")
            return redirect(url_for("admin_dashboard"))

        record_failed_login("admin", email)
        flash("Invalid email or password.", "error")

    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor()

    stats = {}
    cursor.execute("SELECT COUNT(*) FROM customers")
    stats["customers"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sellers")
    stats["sellers"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sellers WHERE status = 'pending'")
    stats["pending_sellers"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM orders")
    stats["orders"] = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status = 'delivered'")
    stats["sales"] = float(cursor.fetchone()[0])
    cursor.execute("SELECT COUNT(*) FROM complaints WHERE status = 'pending'")
    stats["pending_complaints"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM contact_messages WHERE status = 'new'")
    stats["new_messages"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM newsletter_subscribers")
    stats["subscribers"] = cursor.fetchone()[0]

    # Pending sellers first, then approved, then blocked
    cursor.execute(
        "SELECT seller_id, shop_name, owner_name, email, status FROM sellers "
        "ORDER BY FIELD(status, 'pending', 'approved', 'blocked'), created_at DESC"
    )
    sellers = cursor.fetchall()

    # Whole-platform delivered orders and sales for each of the last 7 days (Indian dates), zero if none
    cursor.execute(
        "SELECT DATE(delivered_at), COUNT(*), COALESCE(SUM(total_amount), 0) "
        "FROM orders WHERE status = 'delivered' AND delivered_at >= CURDATE() - INTERVAL 6 DAY "
        "GROUP BY DATE(delivered_at)")
    found = {row[0]: row for row in cursor.fetchall()}
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    daily = []
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        row = found.get(day)
        daily.append({"label": day.strftime("%a"), "orders": int(row[1]) if row else 0,
                      "sales": float(row[2]) if row else 0.0})

    # FVRS base score of the top shops (the same numbers as the FVRS page; the maximum is 90)
    ranking = compute_base_scores(cursor)
    fvrs_bars = [{"shop_name": r["shop_name"], "base_score": r["base_score"],
                  "width": round(r["base_score"] / 90 * 100)} for r in ranking[:8]]

    cursor.close()
    conn.close()
    return render_template("admin_dashboard.html", stats=stats, sellers=sellers,
                           daily=daily, fvrs_bars=fvrs_bars)


@app.route("/admin/seller/<int:seller_id>/<action>", methods=["POST"])
def admin_seller_action(seller_id, action):
    if not admin_logged_in():
        return redirect(url_for("admin_login"))

    if action == "approve":
        new_status = "approved"
    elif action == "block":
        new_status = "blocked"
    else:
        flash("Unknown action.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE sellers SET status = %s WHERE seller_id = %s", (new_status, seller_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

    flash("Seller status changed to " + new_status + ".", "success")
    return redirect(url_for("admin_dashboard"))


# =====================================================
# SELLER: PRODUCT MANAGEMENT
# =====================================================
def seller_logged_in():
    return "seller_id" in session


IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}


def looks_like_image(data, ext):
    """Checks the first bytes of the file, so a renamed .exe cannot pass as a .jpg."""
    if ext == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def save_image(file):
    """Saves an uploaded image INSIDE THE DATABASE. Returns (path, error_message).
    The path looks like 'db/12'. The places that call this function stay unchanged."""
    if file is None or file.filename == "":
        return None, None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, "Only png, jpg, jpeg, gif or webp images are allowed."
    data = file.read()
    if not data or not looks_like_image(data, ext):
        return None, "That file is not a valid image."
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO image_store (mime_type, image_data) VALUES (%s, %s)",
                   (IMAGE_MIME[ext], data))
    image_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    return "db/" + str(image_id), None


def read_product_form():
    """Reads and checks the product form. Returns (data, error_message)."""
    name = request.form["product_name"].strip()
    category = request.form["category"]
    description = request.form["description"].strip()
    try:
        price = round(float(request.form["price"]), 2)
        stock = int(request.form["stock_quantity"])
    except ValueError:
        return None, "Price and stock must be valid numbers."
    if not name:
        return None, "Product name is required."
    if category not in CATEGORIES:
        return None, "Please choose a valid category."
    if price < 0 or stock < 0:
        return None, "Price and stock cannot be negative."
    return {"name": name, "category": category, "description": description,
            "price": price, "stock": stock}, None


@app.route("/seller/products")
def seller_products():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT p.product_id, p.product_name, p.category, p.price, p.stock_quantity, "
        "p.is_active, "
        "(SELECT image_path FROM product_images WHERE product_id = p.product_id LIMIT 1) "
        "FROM products p WHERE p.seller_id = %s ORDER BY p.created_at DESC",
        (session["seller_id"],),
    )
    products = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("seller_products.html", products=products)


@app.route("/seller/products/add", methods=["GET", "POST"])
def seller_add_product():
    if not seller_logged_in():
        return redirect(url_for("seller_login"))

    if request.method == "POST":
        data, error = read_product_form()
        image_path = None
        if not error:
            image_path, error = save_image(request.files.get("image"))
        if error:
            flash(error, "error")
            return render_template("product_form.html", product=request.form,
                                   categories=CATEGORIES, title="Add product")

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO products (seller_id, product_name, category, description, price, stock_quantity) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session["seller_id"], data["name"], data["category"],
             data["description"], data["price"], data["stock"]),
        )
        new_product_id = cursor.lastrowid        # the id MySQL just generated
        if image_path:
            cursor.execute(
                "INSERT INTO product_images (product_id, image_path) VALUES (%s, %s)",
                (new_product_id, image_path),
            )
        conn.commit()
        cursor.close()
        conn.close()

        flash("Product added.", "success")
        return redirect(url_for("seller_products"))

    return render_template("product_form.html", product={},
                           categories=CATEGORIES, title="Add product")


@app.route("/seller/products/<int:product_id>/edit", methods=["GET", "POST"])
def seller_edit_product(product_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)   # rows come back as {column_name: value}
    cursor.execute(
        "SELECT product_id, product_name, category, description, price, stock_quantity, "
        "(SELECT image_path FROM product_images WHERE product_id = products.product_id LIMIT 1) AS image_path "
        "FROM products WHERE product_id = %s AND seller_id = %s",
        (product_id, session["seller_id"]),
    )
    product = cursor.fetchone()
    if not product:
        cursor.close()
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for("seller_products"))

    if request.method == "POST":
        data, error = read_product_form()
        image_path = None
        if not error:
            image_path, error = save_image(request.files.get("image"))
        if error:
            cursor.close()
            conn.close()
            flash(error, "error")
            return render_template("product_form.html", product=request.form,
                                   categories=CATEGORIES, title="Edit product")

        cursor.execute(
            "UPDATE products SET product_name = %s, category = %s, description = %s, "
            "price = %s, stock_quantity = %s WHERE product_id = %s AND seller_id = %s",
            (data["name"], data["category"], data["description"], data["price"],
             data["stock"], product_id, session["seller_id"]),
        )
        if image_path:    # a new image was uploaded: replace the old one
            cursor.execute("DELETE FROM product_images WHERE product_id = %s", (product_id,))
            cursor.execute(
                "INSERT INTO product_images (product_id, image_path) VALUES (%s, %s)",
                (product_id, image_path),
            )
        conn.commit()
        cursor.close()
        conn.close()
        flash("Product updated.", "success")
        return redirect(url_for("seller_products"))

    cursor.close()
    conn.close()
    return render_template("product_form.html", product=product,
                           categories=CATEGORIES, title="Edit product")


@app.route("/seller/products/<int:product_id>/toggle", methods=["POST"])
def seller_toggle_product(product_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE products SET is_active = NOT is_active WHERE product_id = %s AND seller_id = %s",
        (product_id, session["seller_id"]),
    )
    conn.commit()
    cursor.close()
    conn.close()
    flash("Product visibility changed.", "success")
    return redirect(url_for("seller_products"))


@app.route("/seller/products/<int:product_id>/stock", methods=["POST"])
def seller_update_stock(product_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))

    try:
        new_stock = int(request.form["stock"])
    except ValueError:
        flash("Stock must be a whole number.", "error")
        return redirect(url_for("seller_products"))
    if new_stock < 0:
        flash("Stock cannot be negative.", "error")
        return redirect(url_for("seller_products"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE products SET stock_quantity = %s WHERE product_id = %s AND seller_id = %s",
        (new_stock, product_id, session["seller_id"]),
    )
    conn.commit()
    cursor.close()
    conn.close()
    flash("Stock updated.", "success")
    return redirect(url_for("seller_products"))


# =====================================================
# CUSTOMER: SHOPPING CART (kept in the session)
# The cart looks like {"12": 2, "15": 1}  ->  product_id: quantity
# =====================================================
def customer_logged_in():
    return "customer_id" in session


@app.context_processor
def inject_cart_count():
    """Makes `cart_count` available in every template (used in the top bar)."""
    cart = session.get("cart", {})
    return {"cart_count": sum(cart.values()),
            "contact_email": CONTACT_EMAIL,
            "contact_phone": CONTACT_PHONE,
            "contact_phone_digits": CONTACT_PHONE_DIGITS}


@app.route("/cart/add/<int:product_id>", methods=["POST"])
def cart_add(product_id):
    if not customer_logged_in():
        flash("Please log in as a customer to add items to your cart.", "error")
        return redirect(url_for("login"))

    try:
        qty = int(request.form.get("quantity", 1))
    except ValueError:
        qty = 1
    if qty < 1:
        qty = 1

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT p.stock_quantity FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
        "WHERE p.product_id = %s AND p.is_active = 1 AND s.status = 'approved'",
        (product_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        flash("That product is not available.", "error")
        return redirect(url_for("home"))

    stock = row[0]
    if stock <= 0:
        flash("Sorry, this product is out of stock.", "error")
        return redirect(url_for("product_detail", product_id=product_id))

    cart = session.get("cart", {})
    key = str(product_id)                 # session keys must be text
    new_qty = cart.get(key, 0) + qty

    if new_qty > stock:
        new_qty = stock
        flash("Only " + str(stock) + " available. Your cart now has the maximum.", "error")
    else:
        flash("Added to cart.", "success")

    cart[key] = new_qty
    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/cart")
def cart_view():
    if not customer_logged_in():
        flash("Please log in as a customer to see your cart.", "error")
        return redirect(url_for("login"))

    cart = session.get("cart", {})
    groups = {}          # seller_id -> {"shop_name", "items", "subtotal"}
    grand_total = 0

    if cart:
        ids = [int(k) for k in cart.keys()]
        placeholders = ", ".join(["%s"] * len(ids))   # e.g. "%s, %s, %s"

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT p.product_id, p.product_name, p.price, p.stock_quantity, "
            "s.seller_id, s.shop_name, "
            "(SELECT image_path FROM product_images WHERE product_id = p.product_id LIMIT 1) "
            "FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
            "WHERE p.is_active = 1 AND s.status = 'approved' "
            "AND p.product_id IN (" + placeholders + ")",
            ids,
        )
        found = {r[0]: r for r in cursor.fetchall()}
        cursor.close()
        conn.close()

        new_cart = {}
        removed_any = False
        reduced_any = False
        for pid in ids:
            r = found.get(pid)
            if r is None or r[3] <= 0:        # hidden, blocked seller, or out of stock
                removed_any = True
                continue
            qty = cart[str(pid)]
            if qty > r[3]:                     # stock dropped since it was added
                qty = r[3]
                reduced_any = True
            new_cart[str(pid)] = qty

            line_total = r[2] * qty
            g = groups.setdefault(r[4], {"shop_name": r[5], "items": [], "subtotal": 0})
            g["items"].append({"id": r[0], "name": r[1], "price": r[2], "qty": qty,
                               "stock": r[3], "image": r[6], "line_total": line_total})
            g["subtotal"] += line_total
            grand_total += line_total

        session["cart"] = new_cart
        if removed_any:
            flash("Some items were no longer available and were removed.", "error")
        if reduced_any:
            flash("Some quantities were reduced to match available stock.", "error")

    return render_template("cart.html", groups=groups.values(), grand_total=grand_total)


@app.route("/cart/update/<int:product_id>", methods=["POST"])
def cart_update(product_id):
    if not customer_logged_in():
        return redirect(url_for("login"))

    cart = session.get("cart", {})
    key = str(product_id)
    if key not in cart:
        return redirect(url_for("cart_view"))

    try:
        qty = int(request.form["quantity"])
    except ValueError:
        flash("Quantity must be a whole number.", "error")
        return redirect(url_for("cart_view"))

    if qty <= 0:
        del cart[key]
        flash("Item removed.", "success")
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_quantity FROM products WHERE product_id = %s", (product_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        stock = row[0] if row else 0
        if qty > stock:
            qty = stock
            flash("Only " + str(stock) + " available. Quantity set to the maximum.", "error")
        else:
            flash("Quantity updated.", "success")
        if qty <= 0:
            del cart[key]
        else:
            cart[key] = qty

    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/cart/remove/<int:product_id>", methods=["POST"])
def cart_remove(product_id):
    if not customer_logged_in():
        return redirect(url_for("login"))

    cart = session.get("cart", {})
    cart.pop(str(product_id), None)
    session["cart"] = cart
    flash("Item removed.", "success")
    return redirect(url_for("cart_view"))


# =====================================================
# CUSTOMER: CHECKOUT AND PLACING ORDERS
# =====================================================
class OrderProblem(Exception):
    """Raised when an order cannot be completed (for example, not enough stock)."""
    pass


def load_cart_groups(cart):
    """Reads the cart items from the database, grouped by seller.
    Returns (list_of_groups, grand_total). It does not change anything."""
    if not cart:
        return [], 0

    ids = [int(k) for k in cart.keys()]
    placeholders = ", ".join(["%s"] * len(ids))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT p.product_id, p.product_name, p.price, p.stock_quantity, "
        "s.seller_id, s.shop_name "
        "FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
        "WHERE p.is_active = 1 AND s.status = 'approved' "
        "AND p.product_id IN (" + placeholders + ")",
        ids,
    )
    found = {r[0]: r for r in cursor.fetchall()}
    cursor.close()
    conn.close()

    groups = {}
    grand_total = 0
    for pid in ids:
        r = found.get(pid)
        if r is None or r[3] <= 0:
            continue
        qty = min(cart[str(pid)], r[3])
        line_total = r[2] * qty
        g = groups.setdefault(r[4], {"shop_name": r[5], "items": [], "subtotal": 0})
        g["items"].append({"name": r[1], "price": r[2], "qty": qty, "line_total": line_total})
        g["subtotal"] += line_total
        grand_total += line_total
    return list(groups.values()), grand_total


@app.route("/checkout")
def checkout():
    if not customer_logged_in():
        flash("Please log in as a customer to check out.", "error")
        return redirect(url_for("login"))

    cart = session.get("cart", {})
    groups, grand_total = load_cart_groups(cart)
    if not groups:
        flash("Your cart is empty.", "error")
        return redirect(url_for("cart_view"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT address FROM customers WHERE customer_id = %s",
                   (session["customer_id"],))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    return render_template("checkout.html", groups=groups, grand_total=grand_total,
                           saved_address=row[0] if row else "",
                           localities=LOCALITIES.keys())


@app.route("/place-order", methods=["POST"])
def place_order():
    if not customer_logged_in():
        return redirect(url_for("login"))

    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("cart_view"))

    customer_id = session["customer_id"]
    locality = request.form.get("locality", "")
    house = request.form.get("house", "").strip()

    conn = get_connection()
    cursor = conn.cursor()

    # ----- Work out the delivery address and coordinates -----
    if locality == "":
        cursor.execute(
            "SELECT address, latitude, longitude FROM customers WHERE customer_id = %s",
            (customer_id,),
        )
        delivery_address, delivery_lat, delivery_lon = cursor.fetchone()
    else:
        if locality not in LOCALITIES or not house:
            cursor.close()
            conn.close()
            flash("Please enter your house/flat details for the chosen colony.", "error")
            return redirect(url_for("checkout"))
        delivery_lat, delivery_lon = LOCALITIES[locality]
        delivery_address = house + ", " + locality

    order_ids = []
    try:
        # ----- Step 1: lock each product row and check stock -----
        # FOR UPDATE makes other customers wait until we finish, so two people
        # cannot both buy the last item.
        by_seller = {}    # seller_id -> list of (product_id, quantity, price)
        for key in sorted(cart.keys(), key=int):      # same order every time avoids deadlocks
            product_id = int(key)
            qty = cart[key]
            cursor.execute(
                "SELECT p.price, p.stock_quantity, p.seller_id, p.product_name "
                "FROM products p JOIN sellers s ON s.seller_id = p.seller_id "
                "WHERE p.product_id = %s AND p.is_active = 1 AND s.status = 'approved' "
                "FOR UPDATE OF p",
                (product_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise OrderProblem("An item in your cart is no longer available.")
            price, stock, seller_id, name = row
            if stock < qty:
                raise OrderProblem("Not enough stock for " + name +
                                   ". Only " + str(stock) + " left.")
            by_seller.setdefault(seller_id, []).append((product_id, qty, price))

        # ----- Step 2: one order per seller (decision D1) -----
        for seller_id, items in by_seller.items():
            total = sum(price * qty for (_, qty, price) in items)
            cursor.execute(
                "INSERT INTO orders (customer_id, seller_id, total_amount, delivery_address, "
                "delivery_latitude, delivery_longitude) VALUES (%s, %s, %s, %s, %s, %s)",
                (customer_id, seller_id, total, delivery_address, delivery_lat, delivery_lon),
            )
            order_id = cursor.lastrowid

            for product_id, qty, price in items:
                cursor.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, unit_price) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, product_id, qty, price),
                )
                # ----- Step 3: reduce stock (decision D2) -----
                cursor.execute(
                    "UPDATE products SET stock_quantity = stock_quantity - %s "
                    "WHERE product_id = %s AND stock_quantity >= %s",
                    (qty, product_id, qty),
                )
                if cursor.rowcount != 1:
                    raise OrderProblem("Stock changed while placing your order. Please try again.")
            order_ids.append(order_id)

        conn.commit()          # everything succeeded: make it permanent

    except OrderProblem as problem:
        conn.rollback()        # undo everything done so far
        cursor.close()
        conn.close()
        flash(str(problem), "error")
        return redirect(url_for("cart_view"))
    except Exception:
        conn.rollback()
        cursor.close()
        conn.close()
        raise                  # show the real error so we can fix it

    cursor.close()
    conn.close()

    session.pop("cart", None)
    session["last_orders"] = order_ids
    return redirect(url_for("order_success"))


@app.route("/order-success")
def order_success():
    if not customer_logged_in():
        return redirect(url_for("login"))

    order_ids = session.pop("last_orders", [])
    if not order_ids:
        return redirect(url_for("home"))

    placeholders = ", ".join(["%s"] * len(order_ids))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT o.order_id, s.shop_name, o.total_amount, o.status "
        "FROM orders o JOIN sellers s ON s.seller_id = o.seller_id "
        "WHERE o.customer_id = %s AND o.order_id IN (" + placeholders + ")",
        [session["customer_id"]] + order_ids,
    )
    orders = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("order_success.html", orders=orders)


# =====================================================
# ORDER HISTORY AND ORDER STATUS CHANGES
# =====================================================
# Which status can follow which (the "order lifecycle")
SELLER_TRANSITIONS = {
    "pending": ["accepted", "cancelled"],
    "accepted": ["packed", "cancelled"],
    "packed": ["out_for_delivery", "cancelled"],
    "out_for_delivery": ["delivered"],
}
CUSTOMER_TRANSITIONS = {"pending": ["cancelled"]}    # a customer can only cancel a pending order

BUTTON_LABELS = {
    "accepted": "Accept",
    "packed": "Mark packed",
    "out_for_delivery": "Out for delivery",
    "delivered": "Mark delivered",
    "cancelled": "Cancel order",
}


def fetch_orders(where_clause, params):
    """Returns a list of orders (as dictionaries), each with an 'items' list."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT o.order_id, o.total_amount, o.status, o.order_date, o.delivered_at, "
        "o.delivery_address, c.full_name AS customer_name, c.phone AS customer_phone, "
        "s.shop_name "
        "FROM orders o "
        "JOIN customers c ON c.customer_id = o.customer_id "
        "JOIN sellers s ON s.seller_id = o.seller_id "
        "WHERE " + where_clause + " ORDER BY o.order_date DESC",
        params,
    )
    orders = cursor.fetchall()

    if orders:
        ids = [o["order_id"] for o in orders]
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            "SELECT oi.order_id, p.product_name, oi.quantity, oi.unit_price "
            "FROM order_items oi JOIN products p ON p.product_id = oi.product_id "
            "WHERE oi.order_id IN (" + placeholders + ")",
            ids,
        )
        items_by_order = {}
        for item in cursor.fetchall():
            items_by_order.setdefault(item["order_id"], []).append(item)
        for o in orders:
            o["items"] = items_by_order.get(o["order_id"], [])

    cursor.close()
    conn.close()
    return orders


def change_order_status(order_id, new_status, transitions, seller_id=None, customer_id=None):
    """Changes an order's status safely. Returns (ok, message).
    Cancelling puts the stock back. Delivering records delivered_at."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Lock the order row, and make sure it belongs to the person asking
        if seller_id is not None:
            cursor.execute(
                "SELECT status FROM orders WHERE order_id = %s AND seller_id = %s FOR UPDATE",
                (order_id, seller_id),
            )
        else:
            cursor.execute(
                "SELECT status FROM orders WHERE order_id = %s AND customer_id = %s FOR UPDATE",
                (order_id, customer_id),
            )
        row = cursor.fetchone()
        if row is None:
            return False, "Order not found."

        current = row[0]
        if new_status not in transitions.get(current, []):
            return False, "This order cannot be changed from '" + current + "' to '" + new_status + "'."

        if new_status == "cancelled":
            # Put the stock back (decision D2)
            cursor.execute(
                "SELECT product_id, quantity FROM order_items WHERE order_id = %s", (order_id,)
            )
            for product_id, qty in cursor.fetchall():
                cursor.execute(
                    "UPDATE products SET stock_quantity = stock_quantity + %s WHERE product_id = %s",
                    (qty, product_id),
                )

        if new_status == "delivered":
            cursor.execute(
                "UPDATE orders SET status = %s, delivered_at = NOW() WHERE order_id = %s",
                (new_status, order_id),
            )
        else:
            cursor.execute(
                "UPDATE orders SET status = %s WHERE order_id = %s", (new_status, order_id)
            )

        conn.commit()
        if new_status in ("delivered", "cancelled"):
            refresh_metrics()        # FVRS numbers change when orders are delivered or cancelled
        return True, "Order #" + str(order_id) + " is now: " + new_status + "."
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# ---------- Customer side ----------
@app.route("/my-orders")
def my_orders():
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))
    orders = fetch_orders("o.customer_id = %s", (session["customer_id"],))
    return render_template("my_orders.html", orders=orders)


@app.route("/my-orders/<int:order_id>/cancel", methods=["POST"])
def customer_cancel_order(order_id):
    if not customer_logged_in():
        return redirect(url_for("login"))
    ok, message = change_order_status(order_id, "cancelled", CUSTOMER_TRANSITIONS,
                                      customer_id=session["customer_id"])
    flash(message, "success" if ok else "error")
    return redirect(url_for("my_orders"))


# ---------- Seller side ----------
@app.route("/seller/orders")
def seller_orders():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))
    orders = fetch_orders(
        "o.seller_id = %s AND o.status NOT IN ('delivered', 'cancelled')",
        (session["seller_id"],),
    )
    return render_template("seller_orders.html", orders=orders, page_title="Current orders",
                           show_actions=True, transitions=SELLER_TRANSITIONS,
                           labels=BUTTON_LABELS)


@app.route("/seller/orders/history")
def seller_order_history():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))
    orders = fetch_orders(
        "o.seller_id = %s AND o.status IN ('delivered', 'cancelled')",
        (session["seller_id"],),
    )
    return render_template("seller_orders.html", orders=orders, page_title="Order history",
                           show_actions=False, transitions=SELLER_TRANSITIONS,
                           labels=BUTTON_LABELS)


@app.route("/seller/orders/<int:order_id>/status", methods=["POST"])
def seller_order_status(order_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))
    new_status = request.form.get("new_status", "")
    ok, message = change_order_status(order_id, new_status, SELLER_TRANSITIONS,
                                      seller_id=session["seller_id"])
    flash(message, "success" if ok else "error")
    return redirect(url_for("seller_orders"))


# =====================================================
# REVIEWS AND COMPLAINTS
# =====================================================
COMPLAINT_TYPES = [
    ("late_delivery", "Late delivery"),
    ("wrong_product", "Wrong product"),
    ("damaged_product", "Damaged product"),
    ("seller_misconduct", "Seller misconduct"),
    ("other", "Other"),
]


def parse_rating(value):
    """Turns the form value into a whole number 1-5, or None if it is invalid."""
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 5 else None


@app.route("/orders/<int:order_id>/review")
def review_page(order_id):
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))

    customer_id = session["customer_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT o.order_id, o.status, s.shop_name FROM orders o "
        "JOIN sellers s ON s.seller_id = o.seller_id "
        "WHERE o.order_id = %s AND o.customer_id = %s",
        (order_id, customer_id),
    )
    order = cursor.fetchone()
    if not order:
        cursor.close()
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("my_orders"))
    if order["status"] != "delivered":
        cursor.close()
        conn.close()
        flash("You can review an order only after it has been delivered.", "error")
        return redirect(url_for("my_orders"))

    cursor.execute("SELECT rating, review_text FROM seller_reviews WHERE order_id = %s", (order_id,))
    seller_review = cursor.fetchone()

    # LEFT JOIN: every product in the order, plus this customer's review if there is one
    cursor.execute(
        "SELECT oi.product_id, p.product_name, pr.rating, pr.review_text "
        "FROM order_items oi "
        "JOIN products p ON p.product_id = oi.product_id "
        "LEFT JOIN product_reviews pr ON pr.product_id = oi.product_id AND pr.customer_id = %s "
        "WHERE oi.order_id = %s",
        (customer_id, order_id),
    )
    products = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("review_page.html", order=order, seller_review=seller_review,
                           products=products)


@app.route("/review/seller/<int:order_id>", methods=["POST"])
def submit_seller_review(order_id):
    if not customer_logged_in():
        return redirect(url_for("login"))

    rating = parse_rating(request.form.get("rating"))
    text = request.form.get("review_text", "").strip()
    if rating is None:
        flash("Please choose a rating from 1 to 5.", "error")
        return redirect(url_for("review_page", order_id=order_id))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT seller_id FROM orders WHERE order_id = %s AND customer_id = %s AND status = 'delivered'",
        (order_id, session["customer_id"]),
    )
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        flash("You can only review delivered orders that are yours.", "error")
        return redirect(url_for("my_orders"))

    cursor.execute("SELECT review_id FROM seller_reviews WHERE order_id = %s", (order_id,))
    if cursor.fetchone():
        flash("You have already reviewed the seller for this order.", "error")
    else:
        cursor.execute(
            "INSERT INTO seller_reviews (customer_id, seller_id, order_id, rating, review_text) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session["customer_id"], row[0], order_id, rating, text),
        )
        conn.commit()
        flash("Thank you! Your seller review was saved.", "success")
    cursor.close()
    conn.close()
    return redirect(url_for("review_page", order_id=order_id))


@app.route("/review/product/<int:order_id>/<int:product_id>", methods=["POST"])
def submit_product_review(order_id, product_id):
    if not customer_logged_in():
        return redirect(url_for("login"))

    rating = parse_rating(request.form.get("rating"))
    text = request.form.get("review_text", "").strip()
    if rating is None:
        flash("Please choose a rating from 1 to 5.", "error")
        return redirect(url_for("review_page", order_id=order_id))

    conn = get_connection()
    cursor = conn.cursor()
    # The product must be part of a delivered order that belongs to this customer
    cursor.execute(
        "SELECT oi.order_item_id FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
        "WHERE o.order_id = %s AND o.customer_id = %s AND o.status = 'delivered' "
        "AND oi.product_id = %s",
        (order_id, session["customer_id"], product_id),
    )
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        flash("You can only review products from your delivered orders.", "error")
        return redirect(url_for("my_orders"))

    cursor.execute(
        "SELECT review_id FROM product_reviews WHERE customer_id = %s AND product_id = %s",
        (session["customer_id"], product_id),
    )
    if cursor.fetchone():
        flash("You have already reviewed this product.", "error")
    else:
        cursor.execute(
            "INSERT INTO product_reviews (customer_id, product_id, rating, review_text) "
            "VALUES (%s, %s, %s, %s)",
            (session["customer_id"], product_id, rating, text),
        )
        conn.commit()
        flash("Thank you! Your product review was saved.", "success")
    cursor.close()
    conn.close()
    return redirect(url_for("review_page", order_id=order_id))


# ---------- Complaints: customer ----------
@app.route("/orders/<int:order_id>/complaint", methods=["GET", "POST"])
def complaint_form(order_id):
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT seller_id, status FROM orders WHERE order_id = %s AND customer_id = %s",
        (order_id, session["customer_id"]),
    )
    order = cursor.fetchone()
    if not order:
        cursor.close()
        conn.close()
        flash("Order not found.", "error")
        return redirect(url_for("my_orders"))
    if order[1] == "cancelled":
        cursor.close()
        conn.close()
        flash("You cannot complain about a cancelled order.", "error")
        return redirect(url_for("my_orders"))

    if request.method == "POST":
        ctype = request.form.get("complaint_type", "")
        text = request.form.get("complaint_text", "").strip()
        valid_types = [t[0] for t in COMPLAINT_TYPES]

        if ctype not in valid_types or len(text) < 5:
            cursor.close()
            conn.close()
            flash("Please choose a type and describe the problem (at least 5 characters).", "error")
            return render_template("complaint_form.html", order_id=order_id,
                                   types=COMPLAINT_TYPES)

        cursor.execute(
            "SELECT complaint_id FROM complaints WHERE order_id = %s AND complaint_type = %s "
            "AND status = 'pending'",
            (order_id, ctype),
        )
        if cursor.fetchone():
            cursor.close()
            conn.close()
            flash("You already have a pending complaint of this type for this order.", "error")
            return redirect(url_for("my_complaints"))

        cursor.execute(
            "INSERT INTO complaints (customer_id, seller_id, order_id, complaint_type, complaint_text) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session["customer_id"], order[0], order_id, ctype, text),
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash("Your complaint was submitted.", "success")
        return redirect(url_for("my_complaints"))

    cursor.close()
    conn.close()
    return render_template("complaint_form.html", order_id=order_id, types=COMPLAINT_TYPES)


@app.route("/my-complaints")
def my_complaints():
    if not customer_logged_in():
        flash("Please log in as a customer first.", "error")
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT cp.complaint_id, cp.order_id, cp.complaint_type, cp.complaint_text, cp.status, "
        "cp.complaint_date, s.shop_name "
        "FROM complaints cp JOIN sellers s ON s.seller_id = cp.seller_id "
        "WHERE cp.customer_id = %s ORDER BY cp.complaint_date DESC",
        (session["customer_id"],),
    )
    complaints = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("my_complaints.html", complaints=complaints)


# ---------- Seller: reviews and complaints ----------
@app.route("/seller/reviews")
def seller_reviews():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT ROUND(AVG(rating), 2) AS avg_rating, COUNT(*) AS review_count "
        "FROM seller_reviews WHERE seller_id = %s",
        (session["seller_id"],),
    )
    summary = cursor.fetchone()
    cursor.execute(
        "SELECT r.rating, r.review_text, r.review_date, r.order_id, c.full_name "
        "FROM seller_reviews r JOIN customers c ON c.customer_id = r.customer_id "
        "WHERE r.seller_id = %s ORDER BY r.review_date DESC",
        (session["seller_id"],),
    )
    reviews = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("seller_reviews.html", summary=summary, reviews=reviews)


@app.route("/seller/complaints")
def seller_complaints():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT cp.complaint_id, cp.order_id, cp.complaint_type, cp.complaint_text, cp.status, "
        "cp.complaint_date, c.full_name "
        "FROM complaints cp JOIN customers c ON c.customer_id = cp.customer_id "
        "WHERE cp.seller_id = %s ORDER BY cp.complaint_date DESC",
        (session["seller_id"],),
    )
    complaints = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("seller_complaints.html", complaints=complaints)


# ---------- Admin: complaints ----------
@app.route("/admin/complaints")
def admin_complaints():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT cp.complaint_id, cp.order_id, cp.complaint_type, cp.complaint_text, cp.status, "
        "cp.complaint_date, c.full_name AS customer_name, s.shop_name "
        "FROM complaints cp "
        "JOIN customers c ON c.customer_id = cp.customer_id "
        "JOIN sellers s ON s.seller_id = cp.seller_id "
        "ORDER BY FIELD(cp.status, 'pending', 'resolved', 'rejected'), cp.complaint_date DESC"
    )
    complaints = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin_complaints.html", complaints=complaints)


@app.route("/admin/complaints/<int:complaint_id>/<action>", methods=["POST"])
def admin_complaint_action(complaint_id, action):
    if not admin_logged_in():
        return redirect(url_for("admin_login"))

    if action == "resolve":
        new_status = "resolved"
    elif action == "reject":
        new_status = "rejected"
    else:
        flash("Unknown action.", "error")
        return redirect(url_for("admin_complaints"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE complaints SET status = %s WHERE complaint_id = %s",
                   (new_status, complaint_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Complaint marked as " + new_status + ".", "success")
    return redirect(url_for("admin_complaints"))


# =====================================================
# ADMIN: VIEW CUSTOMERS AND ALL ORDERS
# =====================================================
@app.route("/admin/customers")
def admin_customers():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT c.customer_id, c.full_name, c.email, c.phone, c.address, c.created_at, "
        "(SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) AS order_count "
        "FROM customers c ORDER BY c.created_at DESC"
    )
    customers = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin_customers.html", customers=customers)


@app.route("/admin/orders")
def admin_orders():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    orders = fetch_orders("1 = 1", ())     # "1 = 1" is always true, so every order is returned
    return render_template("admin_orders.html", orders=orders)


# =====================================================
# ADMIN: FVRS SCORE TABLE
# =====================================================
@app.route("/admin/fvrs")
def admin_fvrs():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    conn = get_connection()
    cursor = conn.cursor()
    results = compute_base_scores(cursor)
    cursor.close()
    conn.close()
    return render_template("admin_fvrs.html", results=results)


@app.route("/admin/fvrs/save", methods=["POST"])
def admin_fvrs_save():
    if not admin_logged_in():
        return redirect(url_for("admin_login"))

    conn = get_connection()
    cursor = conn.cursor()
    results = compute_base_scores(cursor)
    save_daily_metrics(cursor, results)
    conn.commit()
    cursor.close()
    conn.close()
    flash("Saved today's metrics for " + str(len(results)) + " seller(s).", "success")
    return redirect(url_for("admin_fvrs"))


# =====================================================
# ADMIN: MESSAGES (contact form + newsletter) AND CSV EXPORT
# =====================================================
@app.route("/admin/messages")
def admin_messages():
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))
    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT message_id, full_name, email, message_text, status, created_at "
                   "FROM contact_messages ORDER BY FIELD(status, 'new', 'read'), created_at DESC")
    messages = cursor.fetchall()
    cursor.execute("SELECT email, subscribed_at FROM newsletter_subscribers ORDER BY subscribed_at DESC")
    subscribers = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin_messages.html", messages=messages, subscribers=subscribers)


@app.route("/admin/messages/<int:message_id>/read", methods=["POST"])
def admin_message_read(message_id):
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    ensure_extra_tables()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE contact_messages SET status = 'read' WHERE message_id = %s", (message_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for("admin_messages"))


def csv_safe(value):
    """Text for a CSV cell. A cell starting with = + - or @ could run as a formula when opened in
    Excel, so a quote mark is put in front of it."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def csv_download(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
    # The leading \ufeff makes Excel read the file as UTF-8 (so the rupee sign and Hindi names look right)
    response = Response("\ufeff" + buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=" + filename
    return response


def stamp(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


@app.route("/admin/export/<kind>.csv")
def admin_export(kind):
    if not admin_logged_in():
        flash("Please log in as admin first.", "error")
        return redirect(url_for("admin_login"))

    if kind == "orders":
        rows = []
        for o in fetch_orders("1 = 1", ()):
            items = "; ".join(i["product_name"] + " x " + str(i["quantity"]) for i in o["items"])
            rows.append([o["order_id"], stamp(o["order_date"]), o["customer_name"], o["shop_name"],
                         items, o["total_amount"], o["status"], stamp(o["delivered_at"])])
        return csv_download("lockart_orders.csv",
                            ["Order", "Placed", "Customer", "Shop", "Items", "Total (Rs)", "Status", "Delivered"], rows)

    conn = get_connection()
    cursor = conn.cursor()
    if kind == "customers":
        cursor.execute(
            "SELECT c.customer_id, c.full_name, c.email, c.phone, c.address, c.created_at, "
            "(SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) "
            "FROM customers c ORDER BY c.created_at DESC")
        rows = [[r[0], r[1], r[2], r[3], r[4], r[6], stamp(r[5])] for r in cursor.fetchall()]
        header = ["ID", "Name", "Email", "Phone", "Address", "Orders", "Joined"]
        filename = "lockart_customers.csv"
    elif kind == "complaints":
        cursor.execute(
            "SELECT cp.complaint_id, cp.complaint_date, cp.order_id, c.full_name, s.shop_name, "
            "cp.complaint_type, cp.complaint_text, cp.status "
            "FROM complaints cp JOIN customers c ON c.customer_id = cp.customer_id "
            "JOIN sellers s ON s.seller_id = cp.seller_id ORDER BY cp.complaint_date DESC")
        rows = [[r[0], stamp(r[1]), r[2], r[3], r[4], r[5].replace("_", " "), r[6], r[7]] for r in cursor.fetchall()]
        header = ["ID", "Date", "Order", "Customer", "Shop", "Type", "Details", "Status"]
        filename = "lockart_complaints.csv"
    elif kind == "messages":
        ensure_extra_tables()
        cursor.execute("SELECT message_id, created_at, full_name, email, message_text, status "
                       "FROM contact_messages ORDER BY created_at DESC")
        rows = [[r[0], stamp(r[1]), r[2], r[3], r[4], r[5]] for r in cursor.fetchall()]
        header = ["ID", "Date", "Name", "Email", "Message", "Status"]
        filename = "lockart_messages.csv"
    elif kind == "subscribers":
        ensure_extra_tables()
        cursor.execute("SELECT email, subscribed_at FROM newsletter_subscribers ORDER BY subscribed_at DESC")
        rows = [[r[0], stamp(r[1])] for r in cursor.fetchall()]
        header = ["Email", "Subscribed"]
        filename = "lockart_subscribers.csv"
    else:
        cursor.close()
        conn.close()
        abort(404)
    cursor.close()
    conn.close()
    return csv_download(filename, header, rows)


# =====================================================
# SELLER: SMART DELIVERY GROUPING
# =====================================================
@app.route("/seller/delivery-groups")
def seller_delivery_groups():
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))

    seller_id = session["seller_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Packed orders that are not in any group yet
    cursor.execute(
        "SELECT COUNT(*) AS waiting FROM orders o "
        "WHERE o.seller_id = %s AND o.status = 'packed' "
        "AND o.order_id NOT IN (SELECT order_id FROM grouped_orders)",
        (seller_id,),
    )
    waiting = cursor.fetchone()["waiting"]

    cursor.execute(
        "SELECT group_id, group_date, order_count, status FROM delivery_groups "
        "WHERE seller_id = %s ORDER BY group_id DESC LIMIT 30",
        (seller_id,),
    )
    groups = cursor.fetchall()

    if groups:
        ids = [g["group_id"] for g in groups]
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            "SELECT gr.group_id, o.order_id, o.status, o.total_amount, o.delivery_address, "
            "o.delivery_latitude, o.delivery_longitude, c.full_name AS customer_name "
            "FROM grouped_orders gr "
            "JOIN orders o ON o.order_id = gr.order_id "
            "JOIN customers c ON c.customer_id = o.customer_id "
            "WHERE gr.group_id IN (" + placeholders + ") ORDER BY gr.grouped_order_id",
            ids,
        )
        orders_by_group = {}
        for row in cursor.fetchall():
            orders_by_group.setdefault(row["group_id"], []).append(row)

        for g in groups:
            g["orders"] = orders_by_group.get(g["group_id"], [])
            anchor = g["orders"][0] if g["orders"] else None
            for o in g["orders"]:      # distance from the first stop (the anchor)
                if anchor and o["delivery_latitude"] is not None:
                    o["from_anchor"] = round(haversine_km(
                        float(anchor["delivery_latitude"]), float(anchor["delivery_longitude"]),
                        float(o["delivery_latitude"]), float(o["delivery_longitude"])), 2)
                else:
                    o["from_anchor"] = None
    cursor.close()
    conn.close()

    # One trip per group instead of one trip per order
    trips_saved = sum(max(0, g["order_count"] - 1) for g in groups)
    return render_template("seller_delivery_groups.html", groups=groups, waiting=waiting,
                           trips_saved=trips_saved, radius=GROUP_RADIUS_KM,
                           max_size=MAX_GROUP_SIZE)


@app.route("/seller/delivery-groups/create", methods=["POST"])
def seller_create_delivery_groups():
    if not seller_logged_in():
        return redirect(url_for("seller_login"))

    seller_id = session["seller_id"]
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT order_id, delivery_latitude, delivery_longitude FROM orders "
            "WHERE seller_id = %s AND status = 'packed' "
            "AND delivery_latitude IS NOT NULL AND delivery_longitude IS NOT NULL "
            "AND order_id NOT IN (SELECT order_id FROM grouped_orders) "
            "ORDER BY order_id",
            (seller_id,),
        )
        orders = [{"order_id": r[0], "lat": float(r[1]), "lon": float(r[2])}
                  for r in cursor.fetchall()]

        if not orders:
            flash("No packed orders are waiting to be grouped. Mark orders as 'packed' first.", "error")
            return redirect(url_for("seller_delivery_groups"))

        groups = group_orders(orders)
        for group in groups:
            cursor.execute(
                "INSERT INTO delivery_groups (seller_id, group_date, order_count) "
                "VALUES (%s, CURDATE(), %s)",
                (seller_id, len(group)),
            )
            group_id = cursor.lastrowid
            for order in group:
                cursor.execute(
                    "INSERT INTO grouped_orders (group_id, order_id) VALUES (%s, %s)",
                    (group_id, order["order_id"]),
                )
        conn.commit()
        flash("Grouped " + str(len(orders)) + " order(s) into " + str(len(groups)) +
              " delivery trip(s). Trips saved: " + str(len(orders) - len(groups)) + ".", "success")
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("seller_delivery_groups"))


def move_group_orders(group_id, from_status, to_status, new_group_status):
    """Moves every order of a group from one status to the next, then updates the group.
    Returns (ok, message)."""
    seller_id = session["seller_id"]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM delivery_groups WHERE group_id = %s AND seller_id = %s",
                   (group_id, seller_id))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return False, "Delivery group not found."

    expected_group_status = "planned" if new_group_status == "dispatched" else "dispatched"
    if row[0] != expected_group_status:
        cursor.close()
        conn.close()
        return False, "This group cannot be changed from '" + row[0] + "'."

    cursor.execute(
        "SELECT o.order_id FROM grouped_orders gr JOIN orders o ON o.order_id = gr.order_id "
        "WHERE gr.group_id = %s AND o.status = %s",
        (group_id, from_status),
    )
    order_ids = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()

    if not order_ids:
        return False, "No orders in this group are in the '" + from_status + "' state."

    for order_id in order_ids:
        change_order_status(order_id, to_status, SELLER_TRANSITIONS, seller_id=seller_id)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE delivery_groups SET status = %s WHERE group_id = %s",
                   (new_group_status, group_id))
    conn.commit()
    cursor.close()
    conn.close()
    return True, ("Group #" + str(group_id) + ": " + str(len(order_ids)) +
                  " order(s) are now '" + to_status + "'.")


@app.route("/seller/delivery-groups/<int:group_id>/dispatch", methods=["POST"])
def seller_dispatch_group(group_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))
    ok, message = move_group_orders(group_id, "packed", "out_for_delivery", "dispatched")
    flash(message, "success" if ok else "error")
    return redirect(url_for("seller_delivery_groups"))


@app.route("/seller/delivery-groups/<int:group_id>/complete", methods=["POST"])
def seller_complete_group(group_id):
    if not seller_logged_in():
        return redirect(url_for("seller_login"))
    ok, message = move_group_orders(group_id, "out_for_delivery", "delivered", "completed")
    flash(message, "success" if ok else "error")
    return redirect(url_for("seller_delivery_groups"))


# =====================================================
# SAFETY, METRICS AND ERROR PAGES
# =====================================================
@app.before_request
def check_seller_still_approved():
    """If an admin blocks a seller who is logged in, log them out on their next click."""
    if "seller_id" in session and request.endpoint != "static":
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM sellers WHERE seller_id = %s", (session["seller_id"],))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row or row[0] != "approved":
            session.clear()
            flash("Your shop is not active. Please contact the admin.", "error")
            return redirect(url_for("seller_login"))


def refresh_metrics():
    """Recalculates the FVRS numbers and saves today's row for every seller.
    Called automatically when an order is delivered or cancelled."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        results = compute_base_scores(cursor)
        save_daily_metrics(cursor, results)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as error:         # never block an order update because of metrics
        print("Could not refresh metrics:", error)


@app.route("/seller/visibility")
def seller_visibility():
    """Shows a seller how their visibility (FVRS) score is made up."""
    if not seller_logged_in():
        flash("Please log in as a seller first.", "error")
        return redirect(url_for("seller_login"))

    conn = get_connection()
    cursor = conn.cursor()
    ranking = compute_base_scores(cursor)
    mine = next((r for r in ranking if r["seller_id"] == session["seller_id"]), None)
    rank = ranking.index(mine) + 1 if mine else None

    cursor.execute(
        "SELECT metric_date, avg_orders_last_7_days, base_score FROM seller_metrics "
        "WHERE seller_id = %s ORDER BY metric_date DESC LIMIT 14",
        (session["seller_id"],),
    )
    history = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("seller_visibility.html", mine=mine, rank=rank,
                           shop_count=len(ranking), history=history)


@app.errorhandler(404)
def page_not_found(error):
    return render_template("error.html", title="Page not found",
                           message="The page you are looking for does not exist."), 404


@app.errorhandler(500)
def server_error(error):
    return render_template("error.html", title="Something went wrong",
                           message="An unexpected error happened. Please go back and try again."), 500


if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION)