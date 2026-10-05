"""
Smart Delivery Grouping (prototype)

Idea: if one seller has several orders going to nearby places, deliver them in
ONE trip instead of one trip per order.

Method (a simple "greedy, anchor-based" grouping):
  1. Take the orders one by one (oldest first).
  2. Look at the existing groups. The first order of a group is its ANCHOR.
  3. If the new order's delivery point is within GROUP_RADIUS_KM of a group's
     anchor, and the group is not full, add it to that group.
  4. Otherwise start a new group with this order as its anchor.

This is a classroom prototype. It does NOT plan the best route, consider road
distances, delivery times or vehicle capacity, so it is not a logistics system.
"""
from fvrs import haversine_km

GROUP_RADIUS_KM = 1.0     # orders closer than this to a group's anchor can join it
MAX_GROUP_SIZE = 5        # one delivery trip carries at most this many orders


def group_orders(orders):
    """orders: a list of dictionaries with the keys 'order_id', 'lat' and 'lon'.
    Returns a list of groups. Each group is a list of orders; the first one is the anchor."""
    groups = []
    for order in orders:
        placed = False
        for group in groups:
            anchor = group[0]
            close_enough = haversine_km(anchor["lat"], anchor["lon"],
                                        order["lat"], order["lon"]) <= GROUP_RADIUS_KM
            if close_enough and len(group) < MAX_GROUP_SIZE:
                group.append(order)
                placed = True
                break
        if not placed:
            groups.append([order])
    return groups