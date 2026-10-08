"""
Simple load test for Lockart. It only READS public pages (home, search, product, shop, shops, FAQ),
so it never changes any data. It needs nothing installed besides Python.

    python loadtest.py http://127.0.0.1:5000            -> 30 virtual users for 30 seconds
    python loadtest.py https://YOUR-SITE.onrender.com 40 30    -> 40 users for 30 seconds
    python loadtest.py http://127.0.0.1:5000 40 20 0           -> stress test: users never wait

Each virtual user opens a page, waits a moment like a real person (0.3 to 1.2 seconds), and opens the next.
Start it a minute after waking the site, so the free server is not still sleeping.
"""
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")
USERS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
SECONDS = int(sys.argv[3]) if len(sys.argv) > 3 else 30
THINK = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0     # 1 = normal waiting between pages, 0 = no waiting (a stress test)

results = []                 # (path, seconds, status)
lock = threading.Lock()


def fetch(path):
    start = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except Exception:
        status = 0           # no answer at all (timeout or connection problem)
    return time.time() - start, status


def find_pages():
    """Reads the home page to learn real product and shop numbers."""
    try:
        with urllib.request.urlopen(BASE + "/", timeout=90) as response:
            html = response.read().decode("utf-8", "ignore")
    except Exception as error:
        print("Cannot open", BASE, "->", error)
        sys.exit(1)
    products = sorted(set(re.findall(r"/product/(\d+)", html)))[:12]
    shops = sorted(set(re.findall(r"/shop/(\d+)", html)))[:6]
    pages = ["/", "/", "/search", "/search?q=milk", "/search?sort=price_asc", "/shops", "/faq"]
    pages += ["/product/" + p for p in products] + ["/shop/" + s for s in shops]
    return pages


def user(pages, stop_at):
    while time.time() < stop_at:
        path = random.choice(pages)
        seconds, status = fetch(path)
        with lock:
            results.append((path, seconds, status))
        time.sleep(random.uniform(0.3, 1.2) * THINK)


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0


pages = find_pages()
print("Testing", BASE, "with", USERS, "users for", SECONDS, "seconds on", len(pages), "pages...")
stop_at = time.time() + SECONDS
threads = [threading.Thread(target=user, args=(pages, stop_at)) for _ in range(USERS)]
began = time.time()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.time() - began

times = [r[1] for r in results]
errors = [r for r in results if r[2] != 200]
print("\nRequests done:     ", len(results), "(" + str(round(len(results) / elapsed, 1)) + " per second)")
print("Errors:            ", len(errors), "(" + str(round(100 * len(errors) / max(1, len(results)), 1)) + "%)")
print("Typical (median):  ", round(percentile(times, 0.5), 2), "seconds")
print("Slow (95th %):     ", round(percentile(times, 0.95), 2), "seconds")
print("Slowest:           ", round(max(times), 2) if times else 0, "seconds")
if errors:
    codes = {}
    for _, _, status in errors:
        codes[status] = codes.get(status, 0) + 1
    print("Error codes:       ", codes, "(0 means no answer / timeout)")
verdict = "GOOD" if not errors and percentile(times, 0.95) < 3 else "NEEDS ATTENTION"
print("Verdict:           ", verdict, "(good = no errors and 95% of pages under 3 seconds)")