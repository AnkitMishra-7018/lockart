"""
FVRS - Fair Visibility Ranking System

Priority score = 0.50 * Order Fairness
               + 0.15 * Seller Rating
               + 0.15 * Completion Rate
               + 0.10 * Distance          (added per customer at search time - next step)
               + 0.10 * Sales Volume

This file computes everything that does NOT depend on the customer
(fairness, rating, completion, sales). Their weighted total is the BASE SCORE,
which can reach at most 90. Distance (10%) is added later for each customer.
"""

import math
import os
import threading
import time

WINDOW_DAYS = 7          # rolling window for the average and sales volume
MAX_DISTANCE_KM = 10     # a shop 10 km or more away gets a distance score of 0
NEUTRAL_DISTANCE_SCORE = 50   # used when we do not know the visitor's location

WEIGHTS = {
    "fairness": 0.50,
    "rating": 0.15,
    "completion": 0.15,
    "distance": 0.10,
    "sales": 0.10,
}

# The public pages (home, search, shop pages) need the ranking on every visit. The numbers only change when an
# order is delivered/cancelled or a shop is approved, so the base scores are remembered for a few seconds.
# This changes only HOW OFTEN the scores are recalculated, never HOW they are calculated.
# Set the environment variable FVRS_CACHE_SECONDS=0 to switch the cache off.
CACHE_SECONDS = float(os.environ.get("FVRS_CACHE_SECONDS", "15"))
_cache = {"time": 0.0, "results": None}
_cache_lock = threading.Lock()


def clear_ranking_cache():
    """Forgets the remembered scores (called when an order or a shop's status changes)."""
    with _cache_lock:
        _cache["results"] = None


NEUTRAL_RATING = 3.5     # starting rating for a seller with no reviews (cold start)
PRIOR_WEIGHT = 5         # the neutral rating counts as if it were 5 reviews


# ------------------------------------------------------------------
# Small maths helpers (they do not touch the database)
# ------------------------------------------------------------------
def normalize_low_is_good(values):
    """Min-max normalization where a LOWER value gives a HIGHER score (0-100).
    Used for the 7-day average: fewer orders -> more fairness points.
    If every seller has the same value, everyone gets 100 (no division by zero)."""
    if not values:
        return {}
    highest = max(values.values())
    lowest = min(values.values())
    if highest == lowest:
        return {key: 100.0 for key in values}
    return {key: (highest - v) / (highest - lowest) * 100 for key, v in values.items()}


def normalize_high_is_good(values):
    """Min-max normalization where a HIGHER value gives a HIGHER score (0-100).
    Used for sales volume. Same safe handling when all values are equal."""
    if not values:
        return {}
    highest = max(values.values())
    lowest = min(values.values())
    if highest == lowest:
        return {key: 100.0 for key in values}
    return {key: (v - lowest) / (highest - lowest) * 100 for key, v in values.items()}


def smoothed_rating(total_stars, review_count):
    """Average rating that starts at the neutral 3.5 and moves toward the real
    average as reviews arrive. With 0 reviews the result is exactly 3.5."""
    return (total_stars + PRIOR_WEIGHT * NEUTRAL_RATING) / (review_count + PRIOR_WEIGHT)


def rating_to_score(rating):
    """Converts a 1-5 rating to a 0-100 score."""
    score = (rating - 1) / 4 * 100
    return max(0.0, min(100.0, score))


# ------------------------------------------------------------------
# Database readers. Each returns a dictionary {seller_id: value}
# for EVERY approved seller (LEFT JOIN keeps sellers with no data).
# ------------------------------------------------------------------
def get_avg_orders_7d(cursor):
    """Average delivered orders per day over the last 7 days.
    Only 'delivered' orders count, using delivered_at. Always divided by 7."""
    cursor.execute(
        "SELECT s.seller_id, COUNT(o.order_id) "
        "FROM sellers s "
        "LEFT JOIN orders o ON o.seller_id = s.seller_id "
        "  AND o.status = 'delivered' "
        "  AND o.delivered_at >= NOW() - INTERVAL %s DAY "
        "WHERE s.status = 'approved' "
        "GROUP BY s.seller_id",
        (WINDOW_DAYS,),
    )
    return {sid: round(count / WINDOW_DAYS, 2) for sid, count in cursor.fetchall()}


def get_sales_7d(cursor):
    """Total rupee value of delivered orders in the last 7 days (the 'sales volume')."""
    cursor.execute(
        "SELECT s.seller_id, COALESCE(SUM(o.total_amount), 0) "
        "FROM sellers s "
        "LEFT JOIN orders o ON o.seller_id = s.seller_id "
        "  AND o.status = 'delivered' "
        "  AND o.delivered_at >= NOW() - INTERVAL %s DAY "
        "WHERE s.status = 'approved' "
        "GROUP BY s.seller_id",
        (WINDOW_DAYS,),
    )
    return {sid: float(total) for sid, total in cursor.fetchall()}


def get_rating_totals(cursor):
    """(sum of stars, number of seller reviews) for each approved seller."""
    cursor.execute(
        "SELECT s.seller_id, COALESCE(SUM(r.rating), 0), COUNT(r.review_id) "
        "FROM sellers s "
        "LEFT JOIN seller_reviews r ON r.seller_id = s.seller_id "
        "WHERE s.status = 'approved' "
        "GROUP BY s.seller_id"
    )
    return {sid: (float(total), int(count)) for sid, total, count in cursor.fetchall()}


def get_completion_counts(cursor):
    """(delivered orders, cancelled orders) for each approved seller, all time."""
    cursor.execute(
        "SELECT s.seller_id, "
        "COALESCE(SUM(o.status = 'delivered'), 0), COALESCE(SUM(o.status = 'cancelled'), 0) "
        "FROM sellers s LEFT JOIN orders o ON o.seller_id = s.seller_id "
        "WHERE s.status = 'approved' "
        "GROUP BY s.seller_id"
    )
    return {sid: (int(d), int(c)) for sid, d, c in cursor.fetchall()}


# ------------------------------------------------------------------
# The base score
# ------------------------------------------------------------------
def compute_base_scores(cursor):
    """Returns a list of dictionaries (one per approved seller), best base score first."""
    cursor.execute(
        "SELECT seller_id, shop_name FROM sellers WHERE status = 'approved' ORDER BY seller_id"
    )
    names = {sid: name for sid, name in cursor.fetchall()}
    if not names:
        return []

    avg_orders = get_avg_orders_7d(cursor)
    sales = get_sales_7d(cursor)
    ratings = get_rating_totals(cursor)
    completion = get_completion_counts(cursor)

    fairness_scores = normalize_low_is_good(avg_orders)     # fewer orders -> higher
    sales_scores = normalize_high_is_good(sales)            # more sales -> higher

    results = []
    for sid, name in names.items():
        total_stars, review_count = ratings.get(sid, (0.0, 0))
        rating = smoothed_rating(total_stars, review_count)         # cold start: 3.5
        rating_score = rating_to_score(rating)

        delivered, cancelled = completion.get(sid, (0, 0))
        if delivered + cancelled == 0:
            completion_rate = 100.0           # no history yet: benefit of the doubt
        else:
            completion_rate = delivered / (delivered + cancelled) * 100

        fairness = fairness_scores.get(sid, 100.0)
        sales_score = sales_scores.get(sid, 0.0)

        base = (WEIGHTS["fairness"] * fairness
                + WEIGHTS["rating"] * rating_score
                + WEIGHTS["completion"] * completion_rate
                + WEIGHTS["sales"] * sales_score)

        results.append({
            "seller_id": sid,
            "shop_name": name,
            "avg_orders": avg_orders.get(sid, 0),
            "sales_volume": round(sales.get(sid, 0.0), 2),
            "review_count": review_count,
            "rating": round(rating, 2),
            "completion_rate": round(completion_rate, 2),
            "fairness_score": round(fairness, 2),
            "rating_score": round(rating_score, 2),
            "sales_score": round(sales_score, 2),
            "base_score": round(base, 2),
        })

    results.sort(key=lambda r: r["base_score"], reverse=True)
    return results


def cached_base_scores(cursor):
    """compute_base_scores(), but reused for CACHE_SECONDS. Always returns fresh copies,
    because the caller adds distance fields to the dictionaries."""
    if CACHE_SECONDS > 0:
        with _cache_lock:
            if _cache["results"] is not None and time.time() - _cache["time"] < CACHE_SECONDS:
                return [dict(r) for r in _cache["results"]]
    results = compute_base_scores(cursor)
    if CACHE_SECONDS > 0:
        with _cache_lock:
            _cache["results"] = [dict(r) for r in results]
            _cache["time"] = time.time()
    return results


def save_daily_metrics(cursor, results):
    """Saves today's numbers into seller_metrics (one row per seller per day).
    Running it twice on the same day updates the rows instead of duplicating them."""
    for r in results:
        cursor.execute(
            "SELECT metric_id FROM seller_metrics WHERE seller_id = %s AND metric_date = CURDATE()",
            (r["seller_id"],),
        )
        existing = cursor.fetchone()
        values = (r["avg_orders"], r["rating"], r["completion_rate"], r["sales_volume"],
                  r["fairness_score"], r["rating_score"], r["sales_score"], r["base_score"])
        if existing:
            cursor.execute(
                "UPDATE seller_metrics SET avg_orders_last_7_days = %s, seller_rating = %s, "
                "completion_rate = %s, sales_volume = %s, fairness_score = %s, "
                "rating_score = %s, sales_score = %s, base_score = %s WHERE metric_id = %s",
                values + (existing[0],),
            )
        else:
            cursor.execute(
                "INSERT INTO seller_metrics (seller_id, metric_date, avg_orders_last_7_days, "
                "seller_rating, completion_rate, sales_volume, fairness_score, rating_score, "
                "sales_score, base_score) VALUES (%s, CURDATE(), %s, %s, %s, %s, %s, %s, %s, %s)",
                (r["seller_id"],) + values,
            )


# ------------------------------------------------------------------
# Distance (Haversine formula) and the final, customer-specific ranking
# ------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance in km between two latitude/longitude points
    (the Haversine formula, which accounts for the curve of the Earth)."""
    earth_radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def distance_to_score(distance_km):
    """0 km -> 100 points, 10 km or more -> 0 points, straight line in between."""
    return max(0.0, 100 * (1 - distance_km / MAX_DISTANCE_KM))


def get_seller_locations(cursor):
    """{seller_id: (latitude, longitude)} for approved sellers that have a location."""
    cursor.execute(
        "SELECT seller_id, latitude, longitude FROM sellers "
        "WHERE status = 'approved' AND latitude IS NOT NULL AND longitude IS NOT NULL"
    )
    return {sid: (float(lat), float(lon)) for sid, lat, lon in cursor.fetchall()}


def rank_sellers_for_customer(cursor, customer_lat=None, customer_lon=None):
    """The complete FVRS priority score for one visitor.

        final score = base score + 0.10 * distance score

    If the visitor's location is unknown (not logged in), everyone gets the same
    neutral distance score, so the ranking depends only on the base score.
    Returns a list of dictionaries, best seller first."""
    results = cached_base_scores(cursor)
    locations = get_seller_locations(cursor)
    knows_location = customer_lat is not None and customer_lon is not None

    for r in results:
        seller_location = locations.get(r["seller_id"])
        if knows_location and seller_location is not None:
            distance = haversine_km(customer_lat, customer_lon, seller_location[0], seller_location[1])
            distance_score = distance_to_score(distance)
            r["distance_km"] = round(distance, 2)
        else:
            distance_score = NEUTRAL_DISTANCE_SCORE
            r["distance_km"] = None
        r["distance_score"] = round(distance_score, 2)
        r["final_score"] = round(r["base_score"] + WEIGHTS["distance"] * distance_score, 2)
        # A shop with no orders and no reviews is shown as a "new shop" (cold start)
        r["is_new"] = r["review_count"] == 0 and r["avg_orders"] == 0

    results.sort(key=lambda r: r["final_score"], reverse=True)
    return results