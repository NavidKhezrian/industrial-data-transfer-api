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

            CREATE TABLE IF NOT EXISTS snapshot_manifests (
                snapshot_id TEXT PRIMARY KEY,
                factory_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                source_database TEXT,
                source_table TEXT NOT NULL,
                query_type TEXT NOT NULL,
                export_strategy TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                database_fingerprint TEXT,
                snapshot_fingerprint TEXT NOT NULL,
                expected_parts INTEGER NOT NULL,
                expected_rows INTEGER NOT NULL,
                received_parts INTEGER NOT NULL DEFAULT 0,
                received_rows INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ignored_missing_batches (
                batch_id TEXT PRIMARY KEY,
                reason TEXT,
                ignored_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_raw_batches_table
            ON raw_batches(factory_id, source_table, schema_version);

            CREATE INDEX IF NOT EXISTS idx_schema_registry_table
            ON schema_registry(factory_id, source_table, schema_version);

            CREATE INDEX IF NOT EXISTS idx_snapshot_manifests_table
            ON snapshot_manifests(factory_id, source_table, schema_version, status);

            CREATE INDEX IF NOT EXISTS idx_ignored_missing_batches_ignored_at
            ON ignored_missing_batches(ignored_at);
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




def upsert_batch(db_path: str | Path, metadata: RawBatchMetadata, storage_path: str, now_iso: str) -> None:
    """Insert a new batch or replace an existing batch during repair.

    This is used when the Receiver metadata row still exists but the Parquet
    file or its sidecar metadata file was manually deleted. The repair upload
    recreates the file and refreshes the metadata record with the checksum of
    the recreated file. The original checksum is kept inside metadata.extra
    under repair_request.original_checksum.
    """
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
            ON CONFLICT(batch_id) DO UPDATE SET
                factory_id = excluded.factory_id,
                machine_id = excluded.machine_id,
                source_table = excluded.source_table,
                query_type = excluded.query_type,
                export_strategy = excluded.export_strategy,
                schema_fingerprint = excluded.schema_fingerprint,
                schema_version = excluded.schema_version,
                database_fingerprint = excluded.database_fingerprint,
                row_count = excluded.row_count,
                checksum_sha256 = excluded.checksum_sha256,
                storage_path = excluded.storage_path,
                metadata_json = excluded.metadata_json,
                uploaded_at = excluded.uploaded_at
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



def list_ignored_missing_batch_ids(db_path: str | Path) -> set[str]:
    """Return batch IDs the user intentionally hid from missing-file warnings."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT batch_id FROM ignored_missing_batches").fetchall()
        return {str(row["batch_id"]) for row in rows}
    finally:
        conn.close()


def ignore_missing_batches(
    db_path: str | Path,
    batch_ids: list[str],
    *,
    reason: str,
    now_iso: str,
) -> int:
    """Mark missing batch files as intentionally ignored.

    This does not delete Receiver metadata and does not mark the batch as
    repaired. It only suppresses future storage warnings for these batch IDs.
    """
    clean_ids = sorted({str(batch_id).strip() for batch_id in batch_ids if str(batch_id).strip()})
    if not clean_ids:
        return 0

    conn = connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO ignored_missing_batches (batch_id, reason, ignored_at)
            VALUES (?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                reason = excluded.reason,
                ignored_at = excluded.ignored_at
            """,
            [(batch_id, reason, now_iso) for batch_id in clean_ids],
        )
        conn.commit()
        return len(clean_ids)
    finally:
        conn.close()


def unignore_missing_batches(db_path: str | Path, batch_ids: list[str]) -> int:
    """Remove ignore markers for batch IDs. Currently used for manual recovery workflows."""
    clean_ids = sorted({str(batch_id).strip() for batch_id in batch_ids if str(batch_id).strip()})
    if not clean_ids:
        return 0

    conn = connect(db_path)
    try:
        conn.executemany(
            "DELETE FROM ignored_missing_batches WHERE batch_id = ?",
            [(batch_id,) for batch_id in clean_ids],
        )
        conn.commit()
        return conn.total_changes
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
    Return the latest legacy full snapshot record per factory/table.

    Multipart full snapshots are validated through snapshot_manifests instead.
    This legacy list is still useful for batches created before the manifest
    update or when manifest transfer is disabled.
    """

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT rowid AS storage_record_id, batch_id, factory_id, machine_id, source_table,
                   query_type, export_strategy, schema_version,
                   row_count, checksum_sha256, storage_path, uploaded_at, metadata_json
            FROM raw_batches
            WHERE query_type = 'full_table_snapshot'
            ORDER BY factory_id, source_table, rowid DESC
            """
        ).fetchall()

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            try:
                extra = json.loads(item.get("metadata_json") or "{}").get("extra") or {}
            except json.JSONDecodeError:
                extra = {}
            if extra.get("snapshot_manifest"):
                continue
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
                   export_strategy, schema_version, row_count, storage_path, uploaded_at,
                   metadata_json
            FROM raw_batches
            ORDER BY uploaded_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def upsert_snapshot_manifest(db_path: str | Path, manifest: dict[str, Any], now_iso: str) -> None:
    conn = connect(db_path)
    try:
        snapshot_id = manifest["snapshot_id"]
        existing = conn.execute(
            "SELECT snapshot_id FROM snapshot_manifests WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE snapshot_manifests
                SET status = ?, manifest_json = ?, updated_at = ?
                WHERE snapshot_id = ?
                """,
                [manifest.get("status", "started"), json.dumps(manifest, default=str), now_iso, snapshot_id],
            )
        else:
            conn.execute(
                """
                INSERT INTO snapshot_manifests (
                    snapshot_id, factory_id, machine_id, source_database, source_table,
                    query_type, export_strategy, schema_fingerprint, schema_version,
                    database_fingerprint, snapshot_fingerprint, expected_parts, expected_rows,
                    received_parts, received_rows, status, manifest_json,
                    started_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, NULL)
                """,
                [
                    snapshot_id,
                    manifest["factory_id"],
                    manifest["machine_id"],
                    manifest.get("source_database"),
                    manifest["source_table"],
                    manifest["query_type"],
                    manifest["export_strategy"],
                    manifest["schema_fingerprint"],
                    int(manifest["schema_version"]),
                    manifest.get("database_fingerprint"),
                    manifest["snapshot_fingerprint"],
                    int(manifest["expected_parts"]),
                    int(manifest["expected_rows"]),
                    manifest.get("status", "started"),
                    json.dumps(manifest, default=str),
                    now_iso,
                    now_iso,
                ],
            )
        conn.commit()
    finally:
        conn.close()


def get_snapshot_manifest(db_path: str | Path, snapshot_id: str) -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM snapshot_manifests WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["manifest"] = json.loads(item.get("manifest_json") or "{}")
        except json.JSONDecodeError:
            item["manifest"] = {}
        return item
    finally:
        conn.close()


def list_snapshot_manifests(db_path: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM snapshot_manifests
            ORDER BY started_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["manifest"] = json.loads(item.get("manifest_json") or "{}")
            except json.JSONDecodeError:
                item["manifest"] = {}
            result.append(item)
        return result
    finally:
        conn.close()


def update_snapshot_manifest_counts(
    db_path: str | Path,
    snapshot_id: str,
    *,
    received_parts: int,
    received_rows: int,
    status: str | None,
    now_iso: str,
    completed_at: str | None = None,
) -> None:
    conn = connect(db_path)
    try:
        if status is None:
            conn.execute(
                """
                UPDATE snapshot_manifests
                SET received_parts = ?, received_rows = ?, updated_at = ?
                WHERE snapshot_id = ?
                """,
                [received_parts, received_rows, now_iso, snapshot_id],
            )
        else:
            conn.execute(
                """
                UPDATE snapshot_manifests
                SET received_parts = ?, received_rows = ?, status = ?, updated_at = ?, completed_at = ?
                WHERE snapshot_id = ?
                """,
                [received_parts, received_rows, status, now_iso, completed_at, snapshot_id],
            )
        conn.commit()
    finally:
        conn.close()


def list_batches_for_snapshot(db_path: str | Path, snapshot_id: str) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT batch_id, factory_id, machine_id, source_table, query_type,
                   export_strategy, schema_version, row_count, checksum_sha256,
                   storage_path, uploaded_at, metadata_json
            FROM raw_batches
            ORDER BY uploaded_at ASC, batch_id ASC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                metadata = json.loads(item.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            extra = metadata.get("extra") or {}
            manifest = extra.get("snapshot_manifest") or {}
            if manifest.get("snapshot_id") == snapshot_id:
                item["metadata"] = metadata
                item["snapshot_manifest"] = manifest
                result.append(item)
        return result
    finally:
        conn.close()
