from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app_common.schemas import RawBatchMetadata


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_batches (
                batch_id TEXT PRIMARY KEY,
                factory_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                query_type TEXT NOT NULL,
                export_strategy TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                database_fingerprint TEXT,
                row_count INTEGER NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factory_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                schema_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(factory_id, source_table, schema_fingerprint)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_batches_table
                ON raw_batches(factory_id, source_table, schema_version);

            CREATE INDEX IF NOT EXISTS idx_schema_registry_table
                ON schema_registry(factory_id, source_table, schema_version);
            """
        )
        conn.commit()
    finally:
        conn.close()


def batch_exists(db_path: str | Path, batch_id: str) -> bool:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT 1 FROM raw_batches WHERE batch_id = ?", [batch_id]).fetchone()
        return row is not None
    finally:
        conn.close()


def register_schema(conn: sqlite3.Connection, metadata: RawBatchMetadata, now_iso: str) -> None:
    if metadata.schema_snapshot is None:
        return
    schema_json = metadata.schema_snapshot.model_dump_json()
    existing = conn.execute(
        """
        SELECT id FROM schema_registry
        WHERE factory_id = ? AND source_table = ? AND schema_fingerprint = ?
        """,
        [metadata.factory_id, metadata.source_table, metadata.schema_fingerprint],
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE schema_registry SET last_seen_at = ? WHERE id = ?",
            [now_iso, existing["id"]],
        )
    else:
        conn.execute(
            """
            INSERT INTO schema_registry (
                factory_id, source_table, schema_fingerprint, schema_version,
                schema_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                metadata.factory_id,
                metadata.source_table,
                metadata.schema_fingerprint,
                metadata.schema_version,
                schema_json,
                now_iso,
                now_iso,
            ],
        )


def insert_batch(db_path: str | Path, metadata: RawBatchMetadata, storage_path: str, now_iso: str) -> None:
    conn = connect(db_path)
    try:
        register_schema(conn, metadata, now_iso)
        conn.execute(
            """
            INSERT INTO raw_batches (
                batch_id, factory_id, machine_id, source_table, query_type,
                export_strategy, schema_fingerprint, schema_version,
                database_fingerprint, row_count, checksum_sha256, storage_path,
                metadata_json, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                metadata.batch_id,
                metadata.factory_id,
                metadata.machine_id,
                metadata.source_table,
                metadata.query_type,
                metadata.export_strategy,
                metadata.schema_fingerprint,
                metadata.schema_version,
                metadata.database_fingerprint,
                metadata.row_count,
                metadata.checksum_sha256,
                storage_path,
                metadata.model_dump_json(),
                now_iso,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def list_batches(db_path: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT batch_id, factory_id, machine_id, source_table, query_type,
                   export_strategy, schema_version, row_count, checksum_sha256,
                   storage_path, uploaded_at
            FROM raw_batches
            ORDER BY uploaded_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_schemas(db_path: str | Path, factory_id: str | None = None, source_table: str | None = None) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        where = []
        params: list[Any] = []
        if factory_id:
            where.append("factory_id = ?")
            params.append(factory_id)
        if source_table:
            where.append("source_table = ?")
            params.append(source_table)
        sql = """
            SELECT factory_id, source_table, schema_fingerprint, schema_version,
                   first_seen_at, last_seen_at, schema_json
            FROM schema_registry
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY factory_id, source_table, schema_version"
        rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["schema"] = json.loads(item.pop("schema_json"))
            result.append(item)
        return result
    finally:
        conn.close()


def get_batch(db_path: str | Path, batch_id: str) -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT batch_id, factory_id, machine_id, source_table, query_type,
                   export_strategy, schema_version, row_count, checksum_sha256,
                   storage_path, uploaded_at, metadata_json
            FROM raw_batches
            WHERE batch_id = ?
            """,
            [batch_id],
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_latest_full_snapshots(db_path: str | Path) -> list[dict[str, Any]]:
    """
    Return the latest full snapshot record per factory/table.

    This is intentionally computed in Python after ordering by uploaded_at and
    batch_id descending. It prevents old manually deleted files from continuing
    to appear as missing after a newer replacement full snapshot has been stored.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT rowid AS storage_record_id, batch_id, factory_id, machine_id, source_table,
                   query_type, export_strategy, schema_version,
                   row_count, checksum_sha256, storage_path, uploaded_at
            FROM raw_batches
            WHERE query_type = 'full_table_snapshot'
            ORDER BY factory_id, source_table, rowid DESC
            """
        ).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            key = (str(item.get("factory_id")), str(item.get("source_table")))
            if key not in latest:
                latest[key] = item
        return list(latest.values())
    finally:
        conn.close()


def list_all_batch_storage_records(db_path: str | Path, limit: int = 1000) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT batch_id, factory_id, source_table, query_type,
                   export_strategy, schema_version, row_count, storage_path, uploaded_at
            FROM raw_batches
            ORDER BY uploaded_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
