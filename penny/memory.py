"""Small local SQLite memory for conversational searches and price observations."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "penny.sqlite3")
SESSION_TTL_S = int(os.environ.get("PENNY_SESSION_TTL", "1800"))
CACHE_TTL_S = int(os.environ.get("PENNY_CACHE_TTL", "600"))


class NullMemory:
    """Stand-in when the local database cannot be opened; Penny keeps answering."""

    def get_session(self, user_id, **kwargs):
        return None

    def save_session(self, *args, **kwargs):
        pass

    def record_search(self, *args, **kwargs):
        return None

    def cached(self, *args, **kwargs):
        return None

    def attach_history(self, result):
        pass

    def history(self, *args, **kwargs):
        return []

    def price_stats(self, *args, **kwargs):
        return None

    def save_watch(self, *args, **kwargs):
        pass

    def watches(self, *args, **kwargs):
        return []

    def all_watches(self, *args, **kwargs):
        return []

    def touch_check(self, *args, **kwargs):
        pass

    def record_check(self, *args, **kwargs):
        return None

    def remove_watch(self, *args, **kwargs):
        return False

    def price_drops(self, *args, **kwargs):
        return []

    def mark_drop(self, *args, **kwargs):
        pass


class Memory:
    def __init__(self, path=None):
        self.path = path or os.environ.get("PENNY_DB_FILE") or DEFAULT_DB_PATH
        self.session_ttl = int(os.environ.get("PENNY_SESSION_TTL", str(SESSION_TTL_S)))
        self.cache_ttl = int(os.environ.get("PENNY_CACHE_TTL", str(CACHE_TTL_S)))
        if self.session_ttl < 0 or self.cache_ttl < 0:
            raise ValueError("PENNY session/cache TTL must not be negative")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        try:
            os.chmod(self.path, 0o600)  # shopping history stays private, like state/*.json
        except OSError:
            pass

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    request TEXT NOT NULL,
                    query TEXT NOT NULL,
                    sort TEXT NOT NULL,
                    budget REAL,
                    updated_at INTEGER NOT NULL,
                    products_json TEXT NOT NULL DEFAULT '[]',
                    turn_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    request TEXT NOT NULL,
                    query TEXT NOT NULL,
                    query_key TEXT NOT NULL,
                    sort TEXT NOT NULL,
                    budget REAL,
                    continued INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    searched_at INTEGER NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS searches_cache
                    ON searches(query_key, sort, complete, searched_at DESC);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY,
                    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                    site TEXT,
                    store TEXT,
                    product_key TEXT NOT NULL,
                    product_id TEXT,
                    name TEXT NOT NULL,
                    price TEXT,
                    old_price TEXT,
                    quantity REAL,
                    card INTEGER NOT NULL,
                    checked_at TEXT,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS observations_product
                    ON observations(site, product_key, search_id);
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    site TEXT,
                    product_key TEXT NOT NULL,
                    product_id TEXT,
                    name TEXT NOT NULL,
                    store TEXT,
                    target_price TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    checked_at INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(user_id, site, product_key)
                );
                CREATE INDEX IF NOT EXISTS watches_product
                    ON watches(site, product_key, active);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "products_json" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN products_json TEXT NOT NULL DEFAULT '[]'")
            if "turn_json" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN turn_json TEXT NOT NULL DEFAULT '{}'")
            watch_columns = {row[1] for row in db.execute("PRAGMA table_info(watches)")}
            if "checked_at" not in watch_columns:
                db.execute("ALTER TABLE watches ADD COLUMN checked_at INTEGER")

    def get_session(self, user_id, max_age=None, now=None):
        max_age = self.session_ttl if max_age is None else max_age
        now = int(time.time() if now is None else now)
        with self._db() as db:
            row = db.execute(
                "SELECT request, query, sort, budget, updated_at, products_json, turn_json "
                "FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or now - row["updated_at"] > max_age:
            return None
        session = dict(row)
        try:
            session["products"] = json.loads(session.pop("products_json"))
        except (TypeError, ValueError):
            session["products"] = []
        try:
            session["turn"] = json.loads(session.pop("turn_json"))
        except (TypeError, ValueError):
            session["turn"] = {}
        return session

    def save_session(self, user_id, request, query, sort, budget, products=None, now=None,
                     turn=None):
        now = int(time.time() if now is None else now)
        products_json = json.dumps(products or [], ensure_ascii=False, separators=(",", ":"))
        turn_json = json.dumps(turn or {}, ensure_ascii=False, separators=(",", ":"))
        with self._db() as db:
            db.execute(
                """
                INSERT INTO sessions(
                    user_id, request, query, sort, budget, updated_at, products_json, turn_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    request=excluded.request, query=excluded.query, sort=excluded.sort,
                    budget=excluded.budget, updated_at=excluded.updated_at,
                    products_json=excluded.products_json, turn_json=excluded.turn_json
                """,
                (user_id, request, query, sort, budget, now, products_json, turn_json),
            )

    @staticmethod
    def product_key(product):
        product_id = product.get("id")
        return (str(product_id) if product_id is not None
                else str(product.get("name", "")).casefold())

    def attach_history(self, result):
        """Attach the last observed price to each product before recording this one."""
        for product in (result.get("products") or []):
            price = product.get("price")
            if not isinstance(price, str) or not isinstance(product.get("name"), str):
                continue
            row = self._last_observation(product.get("site"), self.product_key(product))
            if row is None or not row["price"]:
                continue
            try:
                if Decimal(row["price"]) != Decimal(price):
                    product["previous_price"] = row["price"]
            except (InvalidOperation, TypeError):
                continue

    def _last_observation(self, site, key, before_id=None):
        query = ("SELECT price, checked_at FROM observations "
                 "WHERE site = ? AND product_key = ? AND price IS NOT NULL")
        params = [site, key]
        if before_id is not None:
            query += " AND id < ?"
            params.append(before_id)
        query += " ORDER BY id DESC LIMIT 1"
        with self._db() as db:
            return db.execute(query, params).fetchone()

    def history(self, site, key, limit=5):
        with self._db() as db:
            rows = db.execute(
                "SELECT price, old_price, checked_at FROM observations "
                "WHERE site = ? AND product_key = ? AND price IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (site, key, max(1, min(limit, 20)))).fetchall()
        return [dict(row) for row in rows]

    def price_stats(self, site, key):
        """All-time low/high for one product, with the observation dates."""
        with self._db() as db:
            stats = db.execute(
                "SELECT count(*) AS observations, min(CAST(price AS REAL)) AS low, "
                "max(CAST(price AS REAL)) AS high FROM observations "
                "WHERE site = ? AND product_key = ? AND price IS NOT NULL",
                (site, key)).fetchone()
            if stats is None or not stats["observations"]:
                return None
            low = db.execute(
                "SELECT price, checked_at FROM observations WHERE site = ? AND product_key = ? "
                "AND price IS NOT NULL ORDER BY CAST(price AS REAL) ASC, id ASC LIMIT 1",
                (site, key)).fetchone()
            high = db.execute(
                "SELECT price, checked_at FROM observations WHERE site = ? AND product_key = ? "
                "AND price IS NOT NULL ORDER BY CAST(price AS REAL) DESC, id ASC LIMIT 1",
                (site, key)).fetchone()
        return {"observations": stats["observations"],
                "min": low["price"], "min_checked": low["checked_at"],
                "max": high["price"], "max_checked": high["checked_at"]}

    def save_watch(self, user_id, product, now=None):
        now = int(time.time() if now is None else now)
        price = product.get("price")
        name = product.get("name")
        if price is None or not isinstance(name, str) or not name:
            raise ValueError("watch needs a priced product")
        product_id = product.get("id")
        with self._db() as db:
            db.execute(
                """
                INSERT INTO watches(user_id, site, product_key, product_id, name, store,
                                    target_price, created_at, updated_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, site, product_key) DO UPDATE SET
                    name=excluded.name, store=excluded.store,
                    target_price=excluded.target_price, updated_at=excluded.updated_at,
                    active=1
                """,
                (user_id, product.get("site"), self.product_key(product),
                 str(product_id) if product_id is not None else None, name,
                 product.get("store"), str(price), now, now),
            )

    def watches(self, user_id):
        with self._db() as db:
            rows = db.execute(
                "SELECT id, site, product_key, name, store, target_price, updated_at "
                "FROM watches WHERE user_id = ? AND active = 1 ORDER BY id",
                (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def all_watches(self):
        """Every active watch, least recently checked first, for the scheduler."""
        with self._db() as db:
            rows = db.execute(
                "SELECT id, user_id, site, product_key, name, store, target_price, checked_at "
                "FROM watches WHERE active = 1 ORDER BY COALESCE(checked_at, 0) ASC, id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_check(self, site, product_key, now=None):
        now = int(time.time() if now is None else now)
        with self._db() as db:
            db.execute("UPDATE watches SET checked_at = ? "
                       "WHERE site = ? AND product_key = ? AND active = 1",
                       (now, site, product_key))

    def record_check(self, site, product, now=None):
        """Store a background observation without polluting the search cache."""
        now = int(time.time() if now is None else now)
        name = product.get("name")
        if (not isinstance(name, str) or not name or product.get("price") is None):
            return None
        payload = json.dumps(
            {"products": [product], "sites": [site], "site_urls": {},
             "incomplete": False, "sort": "popularity"},
            ensure_ascii=False, separators=(",", ":"))
        with self._db() as db:
            cursor = db.execute(
                """
                INSERT INTO searches(
                    user_id, request, query, query_key, sort, budget, continued,
                    complete, searched_at, result_json)
                VALUES (0, ?, ?, ?, 'popularity', NULL, 0, 0, ?, ?)
                """,
                (name, name, name.casefold(), now, payload))
            search_id = cursor.lastrowid
            product_id = product.get("id")
            db.execute(
                """
                INSERT INTO observations(
                    search_id, site, store, product_key, product_id, name, price,
                    old_price, quantity, card, checked_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (search_id, site, product.get("store"), self.product_key(product),
                 str(product_id) if product_id is not None else None, name,
                 product.get("price"), product.get("old_price"), product.get("quantity"),
                 int(product.get("card") is True), product.get("checked"),
                 product.get("source")))
        return search_id

    def remove_watch(self, user_id, watch_id):
        with self._db() as db:
            cursor = db.execute("DELETE FROM watches WHERE id = ? AND user_id = ?",
                                (watch_id, user_id))
        return cursor.rowcount > 0

    def price_drops(self, products):
        """Active watches whose product is now cheaper than the watched price."""
        drops = []
        with self._db() as db:
            for product in (products or []):
                price = product.get("price")
                if price is None:
                    continue
                rows = db.execute(
                    "SELECT id, user_id, site, product_key, name, store, target_price "
                    "FROM watches WHERE active = 1 AND site = ? AND product_key = ?",
                    (product.get("site"), self.product_key(product))).fetchall()
                for row in rows:
                    try:
                        if Decimal(price) < Decimal(row["target_price"]):
                            drops.append((dict(row), product))
                    except (InvalidOperation, TypeError):
                        continue
        return drops

    def mark_drop(self, watch_id, price, now=None):
        now = int(time.time() if now is None else now)
        with self._db() as db:
            db.execute("UPDATE watches SET target_price = ?, updated_at = ?, checked_at = ? "
                       "WHERE id = ?", (str(price), now, now, watch_id))

    def record_search(self, user_id, intent, result, now=None):
        now = int(time.time() if now is None else now)
        complete = not bool(result.get("incomplete"))
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self._db() as db:
            cursor = db.execute(
                """
                INSERT INTO searches(
                    user_id, request, query, query_key, sort, budget, continued,
                    complete, searched_at, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, intent.request, intent.query, intent.query.casefold(), intent.sort,
                 intent.budget, int(intent.continued), int(complete), now, payload),
            )
            search_id = cursor.lastrowid
            rows = []
            for product in result.get("products", []):
                name = product.get("name")
                if not isinstance(name, str) or not name:
                    continue
                product_id = product.get("id")
                key = self.product_key(product)
                rows.append((
                    search_id, product.get("site"), product.get("store"), key,
                    str(product_id) if product_id is not None else None, name,
                    product.get("price"), product.get("old_price"), product.get("quantity"),
                    int(product.get("card") is True), product.get("checked"),
                    product.get("source"),
                ))
            db.executemany(
                """
                INSERT INTO observations(
                    search_id, site, store, product_key, product_id, name, price,
                    old_price, quantity, card, checked_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return search_id

    def cached(self, query, sort, max_age=None, now=None):
        max_age = self.cache_ttl if max_age is None else max_age
        now = int(time.time() if now is None else now)
        with self._db() as db:
            row = db.execute(
                """
                SELECT result_json, searched_at FROM searches
                WHERE query_key = ? AND sort = ? AND complete = 1 AND searched_at >= ?
                ORDER BY searched_at DESC LIMIT 1
                """,
                (query.casefold(), sort, now - max_age),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        result["cached_at"] = row["searched_at"]
        return result
