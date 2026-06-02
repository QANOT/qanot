"""Local SQLite cache for Smartup catalog + sale prices.

Smartup `$export` endpoints are heavy (a tenant can have ~20k products) and
rate-limited (`day_range_limit=30`, `request_quant=1` per window). Pulling the
full catalog on every user request is not viable, so `smartup_sync` populates
this cache once and the search/pricelist tools read from it.

Sale prices come from `order$export` (sale documents) — each line carries the
actual sold price + its price type (e.g. wholesale USD vs retail UZS). We keep
the latest price per (product, price_type).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SmartupCache:
    """Synchronous sqlite3 store, wrapped with ``asyncio.to_thread`` for the loop."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── schema ────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id     TEXT PRIMARY KEY,
                    code           TEXT,
                    name           TEXT,
                    short_name     TEXT,
                    article_code   TEXT,
                    barcodes       TEXT,
                    measure_code   TEXT,
                    box_quant      TEXT,
                    category_names TEXT,
                    state          TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
                CREATE INDEX IF NOT EXISTS idx_products_code ON products(code);

                CREATE TABLE IF NOT EXISTS categories (
                    type_id    TEXT PRIMARY KEY,
                    group_code TEXT,
                    group_name TEXT,
                    name       TEXT,
                    code       TEXT
                );

                CREATE TABLE IF NOT EXISTS sale_prices (
                    product_code    TEXT,
                    price_type_code TEXT,
                    price_type_name TEXT,
                    price           REAL,
                    currency        TEXT,
                    date            TEXT,
                    PRIMARY KEY (product_code, price_type_code)
                );
                CREATE INDEX IF NOT EXISTS idx_sale_prices_code ON sale_prices(product_code);

                CREATE TABLE IF NOT EXISTS customers (
                    person_id   TEXT PRIMARY KEY,
                    kind        TEXT,
                    code        TEXT,
                    name        TEXT,
                    short_name  TEXT,
                    phone       TEXT,
                    tin         TEXT,
                    is_client   TEXT,
                    is_supplier TEXT,
                    state       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name);

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    # ── async wrappers ────────────────────────────────────────
    async def replace_products(self, rows: list[dict]) -> int:
        return await asyncio.to_thread(self._replace_products, rows)

    async def replace_categories(self, rows: list[dict]) -> int:
        return await asyncio.to_thread(self._replace_categories, rows)

    async def replace_sale_prices(self, rows: list[dict]) -> int:
        return await asyncio.to_thread(self._replace_sale_prices, rows)

    async def replace_customers(self, rows: list[dict]) -> int:
        return await asyncio.to_thread(self._replace_customers, rows)

    async def search_products(self, query: str = "", category: str = "",
                              price_type_code: str = "", limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._search_products, query, category,
                                       price_type_code, limit)

    async def list_price_types(self) -> list[dict]:
        return await asyncio.to_thread(self._list_price_types)

    async def list_categories(self) -> list[dict]:
        return await asyncio.to_thread(self._list_categories)

    async def search_customers(self, query: str = "", kind: str = "",
                               limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._search_customers, query, kind, limit)

    async def set_meta(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_meta, key, value)

    async def get_meta(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_meta, key)

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats)

    # ── sync implementations ──────────────────────────────────
    def _replace_categories(self, rows: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM categories")
            conn.executemany(
                "INSERT OR REPLACE INTO categories(type_id, group_code, group_name, name, code) "
                "VALUES(?,?,?,?,?)",
                [(r["type_id"], r["group_code"], r["group_name"], r["name"], r["code"]) for r in rows],
            )
            return len(rows)

    def _category_map(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {r["type_id"]: r["name"] for r in conn.execute(
            "SELECT type_id, name FROM categories")}

    def _replace_products(self, rows: list[dict]) -> int:
        with self._connect() as conn:
            cat_map = self._category_map(conn)
            conn.execute("DELETE FROM products")
            payload = []
            for r in rows:
                names = []
                for g in r.get("groups") or []:
                    tid = g.get("type_id")
                    if tid and tid in cat_map:
                        names.append(cat_map[tid])
                payload.append((
                    r.get("product_id"), r.get("code"), r.get("name"),
                    r.get("short_name"), r.get("article_code"),
                    r.get("barcodes"), r.get("measure_code"),
                    r.get("box_quant"), " | ".join(sorted(set(names))),
                    r.get("state"),
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO products"
                "(product_id, code, name, short_name, article_code, barcodes, "
                " measure_code, box_quant, category_names, state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            return len(payload)

    def _replace_sale_prices(self, rows: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM sale_prices")
            conn.executemany(
                "INSERT OR REPLACE INTO sale_prices"
                "(product_code, price_type_code, price_type_name, price, currency, date) "
                "VALUES(?,?,?,?,?,?)",
                [(r["product_code"], r["price_type_code"], r["price_type_name"],
                  r["price"], r["currency"], r["date"]) for r in rows],
            )
            return len(rows)

    def _replace_customers(self, rows: list[dict]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM customers")
            conn.executemany(
                "INSERT OR REPLACE INTO customers"
                "(person_id, kind, code, name, short_name, phone, tin, is_client, "
                " is_supplier, state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(r.get("person_id"), r.get("_kind"), r.get("code"), r.get("name"),
                  r.get("short_name"), r.get("main_phone"), r.get("tin"),
                  r.get("is_client"), r.get("is_supplier"), r.get("state"))
                 for r in rows],
            )
            return len(rows)

    def _search_products(self, query: str, category: str,
                         price_type_code: str, limit: int) -> list[dict]:
        clauses, args = [], []
        if query:
            like = f"%{query.strip()}%"
            clauses.append("(p.name LIKE ? OR p.short_name LIKE ? OR p.article_code LIKE ? "
                           "OR p.code LIKE ? OR p.barcodes LIKE ?)")
            args += [like, like, like, like, like]
        if category:
            clauses.append("p.category_names LIKE ?")
            args.append(f"%{category.strip()}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT p.product_id, p.code, p.name, p.short_name, p.article_code, "
            "       p.barcodes, p.measure_code, p.box_quant, p.category_names "
            f"FROM products p{where} ORDER BY p.name LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            products = [dict(r) for r in conn.execute(sql, args)]
            if not products:
                return products
            codes = [p["code"] for p in products if p["code"]]
            # fetch sale prices for these products in one pass
            price_map: dict[str, list[dict]] = {}
            if codes:
                ph = ",".join("?" * len(codes))
                pargs: list = list(codes)
                ptc_clause = ""
                if price_type_code:
                    ptc_clause = " AND price_type_code = ?"
                    pargs.append(price_type_code)
                for r in conn.execute(
                    f"SELECT product_code, price_type_code, price_type_name, price, "
                    f"currency, date FROM sale_prices WHERE product_code IN ({ph}){ptc_clause}",
                    pargs,
                ):
                    price_map.setdefault(r["product_code"], []).append({
                        "price_type_code": r["price_type_code"],
                        "price_type_name": r["price_type_name"],
                        "price": r["price"], "currency": r["currency"], "date": r["date"],
                    })
            for p in products:
                p["prices"] = price_map.get(p["code"], [])
            return products

    def _list_price_types(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT price_type_code, price_type_name, currency, COUNT(*) AS product_count "
                "FROM sale_prices GROUP BY price_type_code ORDER BY product_count DESC"
            )
            return [dict(r) for r in rows]

    def _list_categories(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.name, c.group_name, c.code, "
                "  (SELECT COUNT(*) FROM products p WHERE p.category_names LIKE '%' || c.name || '%') AS product_count "
                "FROM categories c ORDER BY c.group_name, c.name"
            )
            return [dict(r) for r in rows]

    def _search_customers(self, query: str, kind: str, limit: int) -> list[dict]:
        clauses, args = [], []
        if query:
            like = f"%{query.strip()}%"
            clauses.append("(name LIKE ? OR short_name LIKE ? OR phone LIKE ? OR tin LIKE ? OR code LIKE ?)")
            args += [like, like, like, like, like]
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT person_id, kind, code, name, short_name, phone, tin, "
               f"is_client, is_supplier, state FROM customers{where} ORDER BY name LIMIT ?")
        args.append(int(limit))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    def _get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def _stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            def count(t: str) -> int:
                return conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            return {
                "products": count("products"),
                "categories": count("categories"),
                "sale_prices": count("sale_prices"),
                "products_with_price": conn.execute(
                    "SELECT COUNT(DISTINCT product_code) AS n FROM sale_prices").fetchone()["n"],
                "customers": count("customers"),
                "last_catalog_sync": self._get_meta("last_catalog_sync"),
                "last_price_sync": self._get_meta("last_price_sync"),
            }
