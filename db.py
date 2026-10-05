"""
Database connection helper.

Settings come from ENVIRONMENT VARIABLES, so no password is ever written in the code
(this is what makes it safe to put the project on GitHub).
On your laptop the variables are read from a file named .env (see .env.example).
On the hosting website they are typed into the host's settings page.
"""
import os
import time

import mysql.connector
from mysql.connector import pooling
from dotenv import load_dotenv
from flask import g, has_app_context

load_dotenv()      # reads the .env file if it exists; does nothing on the host

_config = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "local_market"),
}
if os.environ.get("DB_SSL_CA"):                  # online databases (like Aiven) need an encrypted connection
    _config["ssl_ca"] = os.environ["DB_SSL_CA"]

_pool = None


class ManagedConnection:
    """Wraps a pooled connection so that close() is safe to call more than once.
    close() hands the connection back to the pool (it does not really disconnect)."""

    def __init__(self, raw):
        self._raw = raw
        self._closed = False

    def __getattr__(self, name):          # cursor(), commit(), rollback(), ... go to the real connection
        return getattr(self._raw, name)

    def close(self):
        if not self._closed:
            self._closed = True
            self._raw.close()


def get_connection():
    """Gives out a connection from a small pool. A pool re-uses connections,
    which is much faster than connecting again for every page."""
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="market_pool",
            pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
            pool_reset_session=True,
            **_config,
        )
    for _ in range(60):                    # if all connections are busy, wait up to about 6 seconds
        try:
            conn = ManagedConnection(_pool.get_connection())
            break
        except pooling.PoolError:
            time.sleep(0.1)
    else:
        raise RuntimeError("The database is busy. Please try again.")

    if has_app_context():                  # remember it, so it can be returned even after an error
        g.setdefault("_open_connections", []).append(conn)
    return conn


def close_leftover_connections(error=None):
    """Runs at the end of every web request: returns any connection that was not closed."""
    for conn in g.pop("_open_connections", []):
        try:
            conn.close()
        except Exception:
            pass