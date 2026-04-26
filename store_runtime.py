from __future__ import annotations

from typing import Any, Optional
import json
import re
import sqlite3
import time
from pathlib import Path


DATA_DIR = Path("data")
STORES_DIR = DATA_DIR / "stores"
OPS_DB_FILE = DATA_DIR / "operations.sqlite3"
DEFAULT_STORE_ID = "default"
STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

INTEGRATION_DEFAULTS = {
    "mode": "local_json",
    "connector_type": "builtin",
    "db_engine": "",
    "db_host": "",
    "db_port": "",
    "db_name": "",
    "db_user": "",
    "db_password": "",
    "db_path": "",
    "connection_options": "",
    "healthcheck_sql": "SELECT 1",
    "catalog_query": "",
    "order_insert_sql": "",
    "stock_update_sql": "",
    "order_finalize_sql": "",
    "status": "idle",
    "last_error": "",
    "last_healthcheck_at": 0.0,
    "last_catalog_sync_at": 0.0,
    "last_order_sync_at": 0.0,
    "catalog_synced_products": 0,
    "location_supabase_db_url": "",
    "created_at": 0.0,
    "updated_at": 0.0,
}


DATA_DIR.mkdir(parents=True, exist_ok=True)
STORES_DIR.mkdir(parents=True, exist_ok=True)


def normalize_store_id(value: Any, *, default: str = DEFAULT_STORE_ID) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if not STORE_ID_PATTERN.fullmatch(text):
        raise ValueError("store_id invalido")
    return text


def ensure_store_dir(store_id: str) -> Path:
    normalized = normalize_store_id(store_id)
    path = STORES_DIR / normalized
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_settings_path(store_id: str) -> Path:
    normalized = normalize_store_id(store_id)
    if normalized == DEFAULT_STORE_ID:
        return Path("app_settings.json")
    return ensure_store_dir(normalized) / "settings.json"


def store_products_path(store_id: str) -> Path:
    normalized = normalize_store_id(store_id)
    if normalized == DEFAULT_STORE_ID:
        return Path("products.json")
    return ensure_store_dir(normalized) / "products.json"


def ops_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(OPS_DB_FILE), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_ops_db() -> None:
    with ops_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_integrations (
                store_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'local_json',
                connector_type TEXT NOT NULL DEFAULT 'builtin',
                db_engine TEXT NOT NULL DEFAULT '',
                db_host TEXT NOT NULL DEFAULT '',
                db_port TEXT NOT NULL DEFAULT '',
                db_name TEXT NOT NULL DEFAULT '',
                db_user TEXT NOT NULL DEFAULT '',
                db_password TEXT NOT NULL DEFAULT '',
                db_path TEXT NOT NULL DEFAULT '',
                connection_options TEXT NOT NULL DEFAULT '',
                healthcheck_sql TEXT NOT NULL DEFAULT 'SELECT 1',
                catalog_query TEXT NOT NULL DEFAULT '',
                order_insert_sql TEXT NOT NULL DEFAULT '',
                stock_update_sql TEXT NOT NULL DEFAULT '',
                order_finalize_sql TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT NOT NULL DEFAULT '',
                last_healthcheck_at REAL NOT NULL DEFAULT 0,
                last_catalog_sync_at REAL NOT NULL DEFAULT 0,
                last_order_sync_at REAL NOT NULL DEFAULT 0,
                catalog_synced_products INTEGER NOT NULL DEFAULT 0,
                location_supabase_db_url TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS orders (
                store_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                order_status TEXT NOT NULL DEFAULT 'submitted',
                sync_status TEXT NOT NULL DEFAULT 'local_only',
                sync_message TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                external_reference TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                whatsapp_message TEXT NOT NULL DEFAULT '',
                whatsapp_url TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                synced_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (store_id, protocol)
            );

            CREATE TABLE IF NOT EXISTS sync_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT '',
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                due_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sync_jobs_due
                ON sync_jobs (status, due_at);
            CREATE INDEX IF NOT EXISTS idx_orders_store_status
                ON orders (store_id, sync_status, created_at DESC);
            """
        )
        integration_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(store_integrations)").fetchall()
        }
        if "location_supabase_db_url" not in integration_columns:
            conn.execute(
                "ALTER TABLE store_integrations ADD COLUMN location_supabase_db_url TEXT NOT NULL DEFAULT ''"
            )


def _row_to_dict(row: Optional[sqlite3.Row]) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def get_store_integration(store_id: str) -> dict[str, Any]:
    normalized = normalize_store_id(store_id)
    with ops_connection() as conn:
        row = conn.execute("SELECT * FROM store_integrations WHERE store_id = ?", (normalized,)).fetchone()
    merged = dict(INTEGRATION_DEFAULTS)
    merged["store_id"] = normalized
    merged.update(_row_to_dict(row))
    return merged


def save_store_integration(store_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_store_id(store_id)
    current = get_store_integration(normalized)
    merged = dict(current)
    merged.update(updates or {})
    now = time.time()
    created_at = float(current.get("created_at") or 0) or now
    merged["store_id"] = normalized
    merged["created_at"] = created_at
    merged["updated_at"] = now
    with ops_connection() as conn:
        conn.execute(
            """
            INSERT INTO store_integrations (
                store_id, mode, connector_type, db_engine, db_host, db_port, db_name, db_user, db_password,
                db_path, connection_options, healthcheck_sql, catalog_query, order_insert_sql,
                stock_update_sql, order_finalize_sql, status, last_error, last_healthcheck_at,
                last_catalog_sync_at, last_order_sync_at, catalog_synced_products, location_supabase_db_url,
                created_at, updated_at
            ) VALUES (
                :store_id, :mode, :connector_type, :db_engine, :db_host, :db_port, :db_name, :db_user, :db_password,
                :db_path, :connection_options, :healthcheck_sql, :catalog_query, :order_insert_sql,
                :stock_update_sql, :order_finalize_sql, :status, :last_error, :last_healthcheck_at,
                :last_catalog_sync_at, :last_order_sync_at, :catalog_synced_products, :location_supabase_db_url,
                :created_at, :updated_at
            )
            ON CONFLICT(store_id) DO UPDATE SET
                mode=excluded.mode,
                connector_type=excluded.connector_type,
                db_engine=excluded.db_engine,
                db_host=excluded.db_host,
                db_port=excluded.db_port,
                db_name=excluded.db_name,
                db_user=excluded.db_user,
                db_password=excluded.db_password,
                db_path=excluded.db_path,
                connection_options=excluded.connection_options,
                healthcheck_sql=excluded.healthcheck_sql,
                catalog_query=excluded.catalog_query,
                order_insert_sql=excluded.order_insert_sql,
                stock_update_sql=excluded.stock_update_sql,
                order_finalize_sql=excluded.order_finalize_sql,
                status=excluded.status,
                last_error=excluded.last_error,
                last_healthcheck_at=excluded.last_healthcheck_at,
                last_catalog_sync_at=excluded.last_catalog_sync_at,
                last_order_sync_at=excluded.last_order_sync_at,
                catalog_synced_products=excluded.catalog_synced_products,
                location_supabase_db_url=excluded.location_supabase_db_url,
                updated_at=excluded.updated_at
            """,
            merged,
        )
    return get_store_integration(normalized)


def integration_admin_view(config: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "integration_mode": str(config.get("mode", INTEGRATION_DEFAULTS["mode"])),
        "integration_connector_type": str(config.get("connector_type", INTEGRATION_DEFAULTS["connector_type"])),
        "integration_db_engine": str(config.get("db_engine", "")),
        "integration_db_host": str(config.get("db_host", "")),
        "integration_db_port": str(config.get("db_port", "")),
        "integration_db_name": str(config.get("db_name", "")),
        "integration_db_user": str(config.get("db_user", "")),
        "integration_db_password": "",
        "integration_db_password_configured": bool(str(config.get("db_password", "")).strip()),
        "integration_db_path": str(config.get("db_path", "")),
        "integration_connection_options": str(config.get("connection_options", "")),
        "integration_healthcheck_sql": str(config.get("healthcheck_sql", INTEGRATION_DEFAULTS["healthcheck_sql"])),
        "integration_catalog_query": str(config.get("catalog_query", "")),
        "integration_order_insert_sql": str(config.get("order_insert_sql", "")),
        "integration_stock_update_sql": str(config.get("stock_update_sql", "")),
        "integration_order_finalize_sql": str(config.get("order_finalize_sql", "")),
        "integration_status": str(config.get("status", "idle")),
        "integration_last_error": str(config.get("last_error", "")),
        "integration_last_healthcheck_at": float(config.get("last_healthcheck_at", 0) or 0),
        "integration_last_catalog_sync_at": float(config.get("last_catalog_sync_at", 0) or 0),
        "integration_last_order_sync_at": float(config.get("last_order_sync_at", 0) or 0),
        "integration_catalog_synced_products": int(config.get("catalog_synced_products", 0) or 0),
        "location_supabase_db_url": "",
        "location_supabase_db_url_configured": bool(str(config.get("location_supabase_db_url", "")).strip()),
    }
    return payload


def create_order_record(
    store_id: str,
    protocol: str,
    payload: dict[str, Any],
    whatsapp_message: str,
    whatsapp_url: str,
    sync_status: str,
    sync_message: str,
) -> dict[str, Any]:
    normalized = normalize_store_id(store_id)
    now = time.time()
    row = {
        "store_id": normalized,
        "protocol": str(protocol),
        "order_status": "submitted",
        "sync_status": str(sync_status or "local_only"),
        "sync_message": str(sync_message or ""),
        "attempts": 0,
        "external_reference": "",
        "payload_json": json.dumps(payload or {}, ensure_ascii=False),
        "whatsapp_message": str(whatsapp_message or ""),
        "whatsapp_url": str(whatsapp_url or ""),
        "last_error": "",
        "created_at": now,
        "updated_at": now,
        "synced_at": now if str(sync_status) == "synced" else 0.0,
    }
    with ops_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO orders (
                store_id, protocol, order_status, sync_status, sync_message, attempts, external_reference,
                payload_json, whatsapp_message, whatsapp_url, last_error, created_at, updated_at, synced_at
            ) VALUES (
                :store_id, :protocol, :order_status, :sync_status, :sync_message, :attempts, :external_reference,
                :payload_json, :whatsapp_message, :whatsapp_url, :last_error, :created_at, :updated_at, :synced_at
            )
            """,
            row,
        )
    return get_order_record(normalized, protocol)


def get_order_record(store_id: str, protocol: str) -> dict[str, Any]:
    normalized = normalize_store_id(store_id)
    with ops_connection() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE store_id = ? AND protocol = ?",
            (normalized, str(protocol)),
        ).fetchone()
    if row is None:
        return {}
    payload = dict(row)
    try:
        payload["payload"] = json.loads(payload.get("payload_json", "{}") or "{}")
    except json.JSONDecodeError:
        payload["payload"] = {}
    return payload


def update_order_record(store_id: str, protocol: str, updates: dict[str, Any]) -> dict[str, Any]:
    current = get_order_record(store_id, protocol)
    if not current:
        return {}
    merged = dict(current)
    merged.update(updates or {})
    merged["payload_json"] = json.dumps(merged.get("payload", current.get("payload", {})), ensure_ascii=False)
    merged["updated_at"] = time.time()
    with ops_connection() as conn:
        conn.execute(
            """
            UPDATE orders SET
                order_status = :order_status,
                sync_status = :sync_status,
                sync_message = :sync_message,
                attempts = :attempts,
                external_reference = :external_reference,
                payload_json = :payload_json,
                whatsapp_message = :whatsapp_message,
                whatsapp_url = :whatsapp_url,
                last_error = :last_error,
                updated_at = :updated_at,
                synced_at = :synced_at
            WHERE store_id = :store_id AND protocol = :protocol
            """,
            {
                "store_id": current["store_id"],
                "protocol": current["protocol"],
                "order_status": str(merged.get("order_status", "submitted")),
                "sync_status": str(merged.get("sync_status", "local_only")),
                "sync_message": str(merged.get("sync_message", "")),
                "attempts": int(merged.get("attempts", 0) or 0),
                "external_reference": str(merged.get("external_reference", "")),
                "payload_json": str(merged.get("payload_json", "{}")),
                "whatsapp_message": str(merged.get("whatsapp_message", "")),
                "whatsapp_url": str(merged.get("whatsapp_url", "")),
                "last_error": str(merged.get("last_error", "")),
                "updated_at": float(merged.get("updated_at", time.time()) or time.time()),
                "synced_at": float(merged.get("synced_at", 0) or 0),
            },
        )
    return get_order_record(store_id, protocol)


def list_orders(store_id: str, *, sync_status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    normalized = normalize_store_id(store_id)
    sql = "SELECT * FROM orders WHERE store_id = ?"
    params: list[Any] = [normalized]
    if sync_status:
        sql += " AND sync_status = ?"
        params.append(str(sync_status))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with ops_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    items = []
    for row in rows:
        payload = dict(row)
        try:
            payload["payload"] = json.loads(payload.get("payload_json", "{}") or "{}")
        except json.JSONDecodeError:
            payload["payload"] = {}
        items.append(payload)
    return items


def list_orders_by_created_range(
    store_id: str,
    *,
    created_from: Optional[float] = None,
    created_to: Optional[float] = None,
    sync_status: Optional[str] = None,
) -> list[dict[str, Any]]:
    normalized = normalize_store_id(store_id)
    sql = "SELECT * FROM orders WHERE store_id = ?"
    params: list[Any] = [normalized]
    if sync_status:
        sql += " AND sync_status = ?"
        params.append(str(sync_status))
    if created_from is not None:
        sql += " AND created_at >= ?"
        params.append(float(created_from))
    if created_to is not None:
        sql += " AND created_at < ?"
        params.append(float(created_to))
    sql += " ORDER BY created_at DESC"
    with ops_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    items = []
    for row in rows:
        payload = dict(row)
        try:
            payload["payload"] = json.loads(payload.get("payload_json", "{}") or "{}")
        except json.JSONDecodeError:
            payload["payload"] = {}
        items.append(payload)
    return items


def count_pending_sync_jobs(store_id: str) -> int:
    normalized = normalize_store_id(store_id)
    with ops_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM sync_jobs WHERE store_id = ? AND status IN ('queued', 'retry', 'running')",
            (normalized,),
        ).fetchone()
    return int(row["total"] if row is not None else 0)


def list_due_sync_jobs(now: Optional[float] = None, limit: int = 10) -> list[dict[str, Any]]:
    current_now = float(now or time.time())
    with ops_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sync_jobs
            WHERE status IN ('queued', 'retry') AND due_at <= ?
            ORDER BY due_at ASC, id ASC
            LIMIT ?
            """,
            (current_now, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_due_sync_jobs(
    now: Optional[float] = None,
    limit: int = 10,
    *,
    stale_running_after_seconds: float = 900.0,
) -> list[dict[str, Any]]:
    current_now = float(now or time.time())
    max_limit = max(1, int(limit))
    stale_after = max(60.0, float(stale_running_after_seconds or 900.0))
    stale_cutoff = current_now - stale_after
    claimed: list[dict[str, Any]] = []
    with ops_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'retry', due_at = ?, updated_at = ?
            WHERE status = 'running' AND updated_at <= ?
            """,
            (current_now, current_now, stale_cutoff),
        )
        rows = conn.execute(
            """
            SELECT * FROM sync_jobs
            WHERE status IN ('queued', 'retry') AND due_at <= ?
            ORDER BY due_at ASC, id ASC
            LIMIT ?
            """,
            (current_now, max_limit),
        ).fetchall()
        for row in rows:
            updated = conn.execute(
                """
                UPDATE sync_jobs
                SET status = 'running', updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retry')
                """,
                (current_now, int(row["id"])),
            )
            if updated.rowcount <= 0:
                continue
            payload = dict(row)
            payload["status"] = "running"
            payload["updated_at"] = current_now
            claimed.append(payload)
    return claimed


def enqueue_sync_job(
    store_id: str,
    protocol: str,
    job_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    due_at: Optional[float] = None,
) -> dict[str, Any]:
    normalized = normalize_store_id(store_id)
    now = time.time()
    effective_due = float(due_at or now)
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with ops_connection() as conn:
        existing = conn.execute(
            """
            SELECT * FROM sync_jobs
            WHERE store_id = ? AND protocol = ? AND job_type = ? AND status IN ('queued', 'retry', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (normalized, str(protocol), str(job_type)),
        ).fetchone()
        if existing is not None:
            job_id = int(existing["id"])
            conn.execute(
                """
                UPDATE sync_jobs
                SET status = 'queued', due_at = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (effective_due, payload_json, now, job_id),
            )
            row = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row is not None else {}

        conn.execute(
            """
            INSERT INTO sync_jobs (
                store_id, protocol, job_type, status, attempts, due_at, last_error, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', 0, ?, '', ?, ?, ?)
            """,
            (normalized, str(protocol), str(job_type), effective_due, payload_json, now, now),
        )
        row = conn.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row is not None else {}


def mark_sync_job_running(job_id: int) -> dict[str, Any]:
    now = time.time()
    with ops_connection() as conn:
        conn.execute(
            "UPDATE sync_jobs SET status = 'running', updated_at = ? WHERE id = ?",
            (now, int(job_id)),
        )
        row = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (int(job_id),)).fetchone()
    return dict(row) if row is not None else {}


def complete_sync_job(job_id: int) -> None:
    with ops_connection() as conn:
        conn.execute("DELETE FROM sync_jobs WHERE id = ?", (int(job_id),))


def fail_sync_job(job_id: int, last_error: str, next_due_at: float) -> dict[str, Any]:
    now = time.time()
    with ops_connection() as conn:
        current = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (int(job_id),)).fetchone()
        attempts = int(current["attempts"] if current is not None else 0) + 1
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'retry', attempts = ?, last_error = ?, due_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (attempts, str(last_error or ""), float(next_due_at), now, int(job_id)),
        )
        row = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (int(job_id),)).fetchone()
    return dict(row) if row is not None else {}


def build_retry_delay_seconds(attempts: int) -> int:
    normalized = max(1, int(attempts or 1))
    return min(3600, 30 * (2 ** min(normalized - 1, 6)))


class StoreConnector:
    def __init__(self, config: dict[str, Any]):
        self.config = dict(config or {})

    def healthcheck(self) -> dict[str, Any]:
        return {"ok": True, "message": "Conector local pronto."}

    def pull_catalog(self) -> list[dict[str, Any]]:
        raise RuntimeError("Conector local nao possui sincronizacao externa.")

    def apply_order(self, order_record: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Conector local nao possui sincronizacao externa.")

    def retry_order(self, order_record: dict[str, Any]) -> dict[str, Any]:
        return self.apply_order(order_record)


class LocalJsonStoreConnector(StoreConnector):
    pass


class ExternalDbStoreConnector(StoreConnector):
    def _engine(self) -> str:
        return str(self.config.get("db_engine") or "").strip().lower()

    def _connect(self) -> sqlite3.Connection:
        engine = self._engine()
        if engine != "sqlite":
            raise RuntimeError(f"Engine {engine or 'nao informado'} ainda nao suportada nesta etapa.")
        db_path = str(self.config.get("db_path") or "").strip()
        if not db_path:
            raise RuntimeError("Configure integration_db_path para usar o conector sqlite.")
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def healthcheck(self) -> dict[str, Any]:
        sql = str(self.config.get("healthcheck_sql") or "SELECT 1").strip() or "SELECT 1"
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
        return {
            "ok": True,
            "message": "Conexao validada com sucesso.",
            "sample": dict(row) if row is not None else {},
        }

    def pull_catalog(self) -> list[dict[str, Any]]:
        sql = str(self.config.get("catalog_query") or "").strip()
        if not sql:
            raise RuntimeError("Configure integration_catalog_query antes de sincronizar o catalogo.")
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def apply_order(self, order_record: dict[str, Any]) -> dict[str, Any]:
        insert_sql = str(self.config.get("order_insert_sql") or "").strip()
        stock_sql = str(self.config.get("stock_update_sql") or "").strip()
        finalize_sql = str(self.config.get("order_finalize_sql") or "").strip()
        if not insert_sql and not stock_sql and not finalize_sql:
            raise RuntimeError("Configure ao menos um SQL de pedido para a loja integrada.")

        payload = dict(order_record.get("payload") or {})
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not items:
            raise RuntimeError("Pedido sem itens para sincronizar.")

        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                order_params = build_order_sql_params(payload, order_record)
                if insert_sql:
                    conn.execute(insert_sql, order_params)
                if stock_sql:
                    for item in items:
                        conn.execute(stock_sql, build_order_sql_params(payload, order_record, item))
                if finalize_sql:
                    conn.execute(finalize_sql, order_params)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "ok": True,
            "message": "Pedido sincronizado no banco externo.",
            "external_reference": str(order_record.get("protocol", "")),
        }


def build_order_sql_params(
    payload: dict[str, Any],
    order_record: dict[str, Any],
    item: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    params = {
        "protocol": str(order_record.get("protocol", "")),
        "store_id": str(order_record.get("store_id", "")),
        "order_total": float(payload.get("order_total", 0) or 0),
        "products_total": float(payload.get("products_total", 0) or 0),
        "delivery_fee": float(payload.get("delivery_fee", 0) or 0),
        "delivery_region": str(payload.get("delivery_region", "")),
        "delivery_address": str(payload.get("delivery_address", "")),
        "payment_method": str(payload.get("payment_method", "")),
        "cash_change_for": float(payload.get("cash_change_for", 0) or 0),
        "items_json": json.dumps(payload.get("items", []), ensure_ascii=False),
        "whatsapp_message": str(order_record.get("whatsapp_message", "")),
        "whatsapp_url": str(order_record.get("whatsapp_url", "")),
        "created_at": float(order_record.get("created_at", time.time()) or time.time()),
        "item_count": len(payload.get("items", []) if isinstance(payload.get("items"), list) else []),
    }
    if item is not None:
        params.update(
            {
                "product_id": str(item.get("id", "")),
                "qty": int(item.get("qty", 0) or 0),
                "unit": str(item.get("unit", "")),
                "description": str(item.get("description", "")),
                "unit_price": float(item.get("unit_price", 0) or 0),
                "subtotal": float(item.get("subtotal", 0) or 0),
            }
        )
    return params


def build_store_connector(config: dict[str, Any]) -> StoreConnector:
    mode = str(config.get("mode") or INTEGRATION_DEFAULTS["mode"]).strip().lower()
    if mode == "external_db":
        return ExternalDbStoreConnector(config)
    return LocalJsonStoreConnector(config)
