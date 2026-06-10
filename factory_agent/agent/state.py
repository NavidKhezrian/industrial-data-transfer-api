from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def empty_state() -> dict[str, Any]:
    return {
        "state_format_version": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": None,
        "database_fingerprint": None,
        "tables": {},
        "events": [],
        "uploaded_batches": [],
    }


def load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = p.with_suffix(p.suffix + ".broken")
        p.replace(backup)
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("state_format_version", 3)
    data.setdefault("tables", {})
    data.setdefault("events", [])
    data.setdefault("uploaded_batches", [])
    return data


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(p)


def get_table_state(state: dict[str, Any], table_name: str) -> dict[str, Any]:
    tables = state.setdefault("tables", {})
    table_state = tables.setdefault(
        table_name,
        {
            "status": "active",
            "schema_fingerprint": None,
            "schema_version": 0,
            "sync_strategy": None,
            "sync_key": None,
            "last_value": None,
            "last_successful_batch": None,
            "last_successful_sync": None,
            "row_count_at_last_snapshot": None,
            "last_full_snapshot_fingerprint": None,
            "last_full_snapshot_batch": None,
            "last_full_snapshot_parquet_path": None,
            "last_full_snapshot_uploaded": False,
        },
    )
    table_state.setdefault("status", "active")
    table_state.setdefault("schema_version", 0)
    table_state.setdefault("row_count_at_last_snapshot", None)
    table_state.setdefault("last_full_snapshot_fingerprint", None)
    table_state.setdefault("last_full_snapshot_batch", None)
    table_state.setdefault("last_full_snapshot_parquet_path", None)
    table_state.setdefault("last_full_snapshot_uploaded", False)
    return table_state


def add_event(state: dict[str, Any], event_type: str, table_name: str | None = None, **payload: Any) -> None:
    events = state.setdefault("events", [])
    events.append(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "table_name": table_name,
            **payload,
        }
    )
    if len(events) > 500:
        del events[:-500]


def mark_table_missing(state: dict[str, Any], table_name: str) -> None:
    table_state = get_table_state(state, table_name)
    table_state["status"] = "missing_in_source"
    table_state["last_seen_missing_at"] = datetime.now(timezone.utc).isoformat()
    add_event(state, "table_missing_in_source", table_name)


def update_table_after_batch(
    state: dict[str, Any],
    *,
    table_name: str,
    schema_fingerprint: str,
    schema_version: int,
    sync_strategy: str,
    sync_key: str | None,
    last_value: Any,
    batch_id: str,
    row_count: int,
    row_count_at_last_snapshot: int | None = None,
    parquet_path: str | None = None,
    uploaded: bool = False,
    full_snapshot_fingerprint: str | None = None,
) -> None:
    table_state = get_table_state(state, table_name)
    table_state.update(
        {
            "status": "active",
            "schema_fingerprint": schema_fingerprint,
            "schema_version": schema_version,
            "sync_strategy": sync_strategy,
            "sync_key": sync_key,
            "last_value": last_value,
            "last_successful_batch": batch_id,
            "last_successful_sync": datetime.now(timezone.utc).isoformat(),
        }
    )
    if row_count_at_last_snapshot is not None:
        table_state["row_count_at_last_snapshot"] = row_count_at_last_snapshot
    if parquet_path:
        table_state["last_parquet_path"] = parquet_path
    if sync_strategy == "full_snapshot" and full_snapshot_fingerprint:
        table_state["last_full_snapshot_fingerprint"] = full_snapshot_fingerprint
        table_state["last_full_snapshot_batch"] = batch_id
        table_state["last_full_snapshot_parquet_path"] = parquet_path
        table_state["last_full_snapshot_uploaded"] = bool(uploaded)
    state.setdefault("uploaded_batches", []).append(
        {
            "batch_id": batch_id,
            "table_name": table_name,
            "schema_version": schema_version,
            "sync_strategy": sync_strategy,
            "row_count": row_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
