from __future__ import annotations

import argparse
import hmac
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.config import AgentConfig, load_config
from agent.exporter import write_metadata_file, write_raw_parquet_batch
from agent.sqlite_reader import (
    choose_sync_strategy,
    connect_readonly,
    get_max_value,
    get_table_content_fingerprint,
    get_table_row_count,
    inspect_database_schema,
    quote_identifier,
    read_incremental_rows,
    read_limited_query,
    readable_db,
)
from agent.state import (
    add_event,
    get_table_state,
    load_state,
    mark_table_missing,
    save_state,
    update_table_after_batch,
)
from agent.uploader import upload_batch
from app_common.schemas import RawBatchMetadata


app = FastAPI(
    title="Factory Agent API",
    version="3.4.0",
    description="Schema-aware raw SQLite replication agent. No data interpretation is performed.",
)

_CONFIG_CACHE: AgentConfig | None = None
_CONFIG_CACHE_PATH: str | None = None


class AgentSyncRequest(BaseModel):
    factory_id: str | None = None
    machine_id: str | None = None
    tables: list[str] | None = None
    max_records_per_table: int | None = Field(default=None, ge=1, le=5_000_000)
    upload: bool = True
    force_full_snapshot: bool = False
    # Receiver-side safety override. If the Receiver detects that previously
    # stored files were manually deleted, it sends the affected table names here.
    force_recreate_tables: list[str] | None = None


class AgentLimitedQueryRequest(BaseModel):
    factory_id: str | None = None
    machine_id: str | None = None
    table: str
    columns: list[str] | None = None
    # Legacy single-filter fields, still supported.
    where_column: str | None = None
    operator: str | None = Field(default=None, description="eq, ne, gt, gte, lt, lte, or contains")
    value: Any = None
    # Preferred custom-query filters. Each item has column, operator, value.
    filters: list[dict[str, Any]] | None = None
    time_column: str | None = None
    start_time: Any = None
    end_time: Any = None
    max_records: int = Field(default=1000, ge=1, le=1_000_000)
    upload: bool = True




class AgentRepairItem(BaseModel):
    batch_id: str
    metadata_json: str
    storage_path: str | None = None
    metadata_path: str | None = None
    source_table: str | None = None
    query_type: str | None = None
    export_strategy: str | None = None
    schema_version: int | None = None
    row_count: int | None = None
    parquet_missing: bool = True
    metadata_missing: bool = True


class AgentRepairRequest(BaseModel):
    items: list[AgentRepairItem] = Field(default_factory=list)
    upload: bool = True


class AgentBatchInfo(BaseModel):
    table_name: str
    status: str
    message: str | None = None
    batch_id: str | None = None
    query_type: str | None = None
    export_strategy: str | None = None
    parquet_path: str | None = None
    metadata_path: str | None = None
    row_count: int | None = None
    schema_version: int | None = None
    schema_fingerprint: str | None = None
    sync_key: str | None = None
    lower_bound: Any = None
    upper_bound: Any = None
    uploaded: bool = False
    upload_response: dict[str, Any] | None = None
    full_snapshot_reason: str | None = None
    schema_changed: bool = False
    snapshot_fingerprint: str | None = None
    previous_batch_id: str | None = None
    snapshot_id: str | None = None
    snapshot_part_number: int | None = None
    snapshot_total_parts: int | None = None
    snapshot_finalized: bool = False
    transfer_request_id: str | None = None
    part_number: int | None = None
    total_parts: int | None = None
    file_name: str | None = None


class AgentSyncResponse(BaseModel):
    status: str
    message: str
    database_fingerprint: str | None = None
    batches: list[AgentBatchInfo] = Field(default_factory=list)
    schema_events: list[dict[str, Any]] = Field(default_factory=list)
    snapshot_manifests: list[dict[str, Any]] = Field(default_factory=list)


def get_config_path() -> str:
    return os.environ.get("AGENT_CONFIG", "config.yaml")


def get_config() -> AgentConfig:
    global _CONFIG_CACHE, _CONFIG_CACHE_PATH
    config_path = get_config_path()
    if _CONFIG_CACHE is None or _CONFIG_CACHE_PATH != config_path:
        _CONFIG_CACHE = load_config(config_path)
        _CONFIG_CACHE_PATH = config_path
    return _CONFIG_CACHE


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def require_agent_api_key(authorization: str | None = Header(default=None)) -> None:
    cfg = get_config()
    token = extract_bearer_token(authorization)
    if token is None or not hmac.compare_digest(token, cfg.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def validate_request_target(cfg: AgentConfig, factory_id: str | None, machine_id: str | None) -> None:
    # Backward-compatible no-op. Older Receiver requests may still include
    # factory_id/machine_id, but this deployment has one SQLite source only.
    return None


def print_config_summary(cfg: AgentConfig) -> None:
    print("Factory Agent configuration")
    print(" source: single SQLite database")
    print(f" sqlite_path: {cfg.sqlite_path}")
    print(f" output_dir: {cfg.output_dir}")
    print(f" state_file: {cfg.state_file}")
    print(f" api_base_url: {cfg.api_base_url}")
    print(f" include_tables: {cfg.include_tables}")
    print(f" exclude_tables: {cfg.exclude_tables}")
    print(f" use_snapshot: {cfg.use_snapshot}")
    print(f" full_snapshot_page_size: {cfg.large_file_transfer.full_snapshot_page_size}")
    print(f" incremental_page_size: {cfg.large_file_transfer.incremental_page_size}")


def post_receiver_json(
    cfg: AgentConfig,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    url = cfg.api_base_url.rstrip("/") + path
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg.receiver_api_key}"},
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Receiver API request failed: {exc}") from exc


def upload_created_batch(cfg: AgentConfig, parquet_path: Path, metadata: RawBatchMetadata) -> dict[str, Any]:
    try:
        return upload_batch(
            api_base_url=cfg.api_base_url,
            api_key=cfg.receiver_api_key,
            parquet_path=parquet_path,
            metadata=metadata,
            timeout_seconds=cfg.large_file_transfer.upload_timeout_seconds,
            retries=cfg.large_file_transfer.upload_retries,
            retry_backoff_seconds=cfg.large_file_transfer.upload_retry_backoff_seconds,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to upload batch to Receiver API: {exc}") from exc


def create_schema_version(table_state: dict[str, Any], schema_fingerprint: str) -> tuple[int, bool]:
    previous = table_state.get("schema_fingerprint")
    previous_version = int(table_state.get("schema_version") or 0)
    if previous == schema_fingerprint and previous_version > 0:
        return previous_version, False
    return previous_version + 1, True


def decide_table_export(
    cfg: AgentConfig,
    *,
    table_name: str,
    table_schema: Any,
    table_state: dict[str, Any],
    force_full_snapshot: bool,
) -> dict[str, Any]:
    selected_strategy, selected_key = choose_sync_strategy(
        table_schema,
        id_candidates=cfg.sync_defaults.id_candidates,
        updated_at_candidates=cfg.sync_defaults.updated_at_candidates,
        timestamp_candidates=cfg.sync_defaults.timestamp_candidates,
    )
    schema_version, schema_changed = create_schema_version(table_state, table_schema.schema_fingerprint)
    previous_strategy = table_state.get("sync_strategy")
    previous_key = table_state.get("sync_key")
    previous_last_value = table_state.get("last_value")
    first_sync = not table_state.get("last_successful_batch")

    if force_full_snapshot:
        return {
            "query_type": "full_table_snapshot",
            "export_strategy": "full_snapshot",
            "sync_key": selected_key,
            "lower_bound": None,
            "schema_version": schema_version,
            "schema_changed": schema_changed,
            "full_snapshot_reason": "force_full_snapshot",
        }

    if first_sync:
        return {
            "query_type": "full_table_snapshot",
            "export_strategy": "full_snapshot" if cfg.sync_defaults.full_snapshot_for_new_tables else selected_strategy,
            "sync_key": selected_key,
            "lower_bound": None,
            "schema_version": schema_version,
            "schema_changed": schema_changed,
            "full_snapshot_reason": "first_sync_or_new_table",
        }

    sync_key_lost = previous_key and previous_key not in [c.name for c in table_schema.columns]
    if sync_key_lost and cfg.sync_defaults.full_snapshot_on_sync_key_loss:
        return {
            "query_type": "full_table_snapshot",
            "export_strategy": "full_snapshot",
            "sync_key": selected_key,
            "lower_bound": None,
            "schema_version": schema_version,
            "schema_changed": schema_changed,
            "full_snapshot_reason": "previous_sync_key_missing_after_schema_change",
        }

    if selected_strategy == "full_snapshot":
        return {
            "query_type": "full_table_snapshot",
            "export_strategy": "full_snapshot",
            "sync_key": selected_key,
            "lower_bound": None,
            "schema_version": schema_version,
            "schema_changed": schema_changed,
            "full_snapshot_reason": "no_reliable_incremental_key",
        }

    return {
        "query_type": "incremental",
        "export_strategy": selected_strategy if selected_strategy else previous_strategy,
        "sync_key": selected_key if selected_key else previous_key,
        "lower_bound": previous_last_value,
        "schema_version": schema_version,
        "schema_changed": schema_changed,
        "full_snapshot_reason": None,
    }


def can_skip_duplicate_full_snapshot(
    table_state: dict[str, Any],
    *,
    current_snapshot_fingerprint: str,
    upload: bool,
) -> bool:
    if table_state.get("last_full_snapshot_fingerprint") != current_snapshot_fingerprint:
        return False
    if upload and table_state.get("last_full_snapshot_uploaded"):
        return True
    previous_path = table_state.get("last_full_snapshot_parquet_path")
    return bool(previous_path and Path(previous_path).exists())


def read_full_table_page(db_path: Path, table: str, *, limit: int, offset: int) -> pd.DataFrame:
    q_table = quote_identifier(table)
    sql = f"SELECT * FROM {q_table} LIMIT ? OFFSET ?"
    conn = connect_readonly(db_path)
    try:
        return pd.read_sql_query(sql, conn, params=[int(limit), int(offset)])
    finally:
        conn.close()


def _timestamp_lower_bound_with_overlap(last_value: Any, timestamp_overlap_seconds: int) -> Any:
    if last_value is None or not timestamp_overlap_seconds:
        return last_value
    value = str(last_value)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt - timedelta(seconds=timestamp_overlap_seconds)).isoformat()
    except ValueError:
        return last_value


def read_incremental_rows_page(
    db_path: Path,
    table: str,
    *,
    sync_key: str,
    strategy: str,
    last_value: Any,
    limit: int,
    offset: int,
    timestamp_overlap_seconds: int = 0,
) -> pd.DataFrame:
    """
    Read one deterministic page of an incremental query.

    max_records_per_table is treated as the total row cap for one request.
    large_file_transfer.incremental_page_size is treated as the Parquet part size.
    This prevents a UI row limit such as 5000 from becoming one 5000-row file.
    """
    q_table = quote_identifier(table)
    q_key = quote_identifier(sync_key)
    params: list[Any] = []

    if last_value is None:
        where_sql = "1=1"
    elif strategy in {"updated_at_incremental", "timestamp_incremental"}:
        value = _timestamp_lower_bound_with_overlap(last_value, timestamp_overlap_seconds)
        where_sql = f"{q_key} >= ?"
        params.append(value)
    else:
        where_sql = f"{q_key} > ?"
        params.append(last_value)

    sql = f"SELECT * FROM {q_table} WHERE {where_sql} ORDER BY {q_key} ASC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    conn = connect_readonly(db_path)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()



_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_file_part(value: Any, *, max_length: int = 80) -> str:
    """Return a readable and filesystem-safe identifier for request and part names."""
    text = _SAFE_FILE_RE.sub("_", str(value).strip()).strip("._-")
    if not text:
        text = "unknown"
    return text[:max_length]


def utc_request_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def create_transfer_request_id(
    *,
    table_name: str,
    schema_version: int,
    transfer_kind: str,
) -> str:
    """
    Create a readable group ID for all Parquet parts created by one table request.

    This ID is stored in metadata.extra and is also used in the filename, so it
    is easy to see that several files belong to the same request.
    """
    safe_table = safe_file_part(table_name, max_length=48)
    safe_kind = safe_file_part(transfer_kind, max_length=12)
    return f"{safe_table}_v{schema_version}_{safe_kind}_req_{utc_request_stamp()}_{uuid.uuid4().hex[:8]}"


def create_part_file_stem(
    *,
    table_name: str,
    schema_version: int,
    query_short: str,
    request_id: str,
    part_number: int,
    total_parts: int,
) -> str:
    width = max(3, len(str(max(1, int(total_parts)))))
    safe_table = safe_file_part(table_name, max_length=40)
    safe_request_id = safe_file_part(request_id, max_length=90)
    suffix = f"part_{int(part_number):0{width}d}_of_{int(total_parts):0{width}d}"
    return safe_file_part(
        f"{safe_table}_v{schema_version}_{query_short}_{safe_request_id}_{suffix}",
        max_length=180,
    )


def create_snapshot_id(cfg: AgentConfig, table_name: str, schema_version: int) -> str:
    return create_transfer_request_id(
        table_name=table_name,
        schema_version=schema_version,
        transfer_kind="full",
    )


def count_incremental_candidate_rows(
    db_path: Path,
    table: str,
    *,
    sync_key: str,
    strategy: str,
    last_value: Any,
    timestamp_overlap_seconds: int = 0,
) -> int:
    """Count rows matching the same incremental predicate used for paging."""
    q_table = quote_identifier(table)
    q_key = quote_identifier(sync_key)
    params: list[Any] = []

    if last_value is None:
        where_sql = "1=1"
    elif strategy in {"updated_at_incremental", "timestamp_incremental"}:
        value = _timestamp_lower_bound_with_overlap(last_value, timestamp_overlap_seconds)
        where_sql = f"{q_key} >= ?"
        params.append(value)
    else:
        where_sql = f"{q_key} > ?"
        params.append(last_value)

    sql = f"SELECT COUNT(*) AS n FROM {q_table} WHERE {where_sql}"
    conn = connect_readonly(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def start_snapshot_manifest(
    cfg: AgentConfig,
    *,
    snapshot_id: str,
    table_schema: Any,
    schema_version: int,
    database_fingerprint: str | None,
    snapshot_fingerprint: str,
    expected_parts: int,
    expected_rows: int,
    query_type: str,
    export_strategy: str,
    full_snapshot_reason: str | None,
) -> dict[str, Any]:
    payload = {
        "snapshot_id": snapshot_id,
        "factory_id": cfg.factory_id,
        "machine_id": cfg.machine_id,
        "source_database": str(cfg.sqlite_path),
        "source_table": table_schema.table_name,
        "query_type": query_type,
        "export_strategy": export_strategy,
        "schema_fingerprint": table_schema.schema_fingerprint,
        "schema_version": schema_version,
        "database_fingerprint": database_fingerprint,
        "snapshot_fingerprint": snapshot_fingerprint,
        "expected_parts": expected_parts,
        "expected_rows": expected_rows,
        "full_snapshot_reason": full_snapshot_reason,
        "transfer_policy": "paged_full_snapshot_manifest",
    }
    return post_receiver_json(
        cfg,
        "/api/v1/uploads/snapshot-manifests/start",
        payload,
        timeout_seconds=cfg.large_file_transfer.manifest_timeout_seconds,
    )


def finalize_snapshot_manifest(
    cfg: AgentConfig,
    *,
    snapshot_id: str,
    expected_parts: int,
    expected_rows: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    payload = {
        "snapshot_id": snapshot_id,
        "expected_parts": expected_parts,
        "expected_rows": expected_rows,
        "snapshot_fingerprint": snapshot_fingerprint,
    }
    return post_receiver_json(
        cfg,
        "/api/v1/uploads/snapshot-manifests/finalize",
        payload,
        timeout_seconds=cfg.large_file_transfer.finalize_timeout_seconds,
    )


def create_and_maybe_upload_incremental_batches(
    cfg: AgentConfig,
    *,
    db_path: Path,
    database_fingerprint: str,
    table_schema: Any,
    decision: dict[str, Any],
    total_limit: int,
    page_size: int,
    upload: bool,
) -> list[AgentBatchInfo]:
    table_name = table_schema.table_name
    strategy = decision["export_strategy"]
    sync_key = decision["sync_key"]
    lower_bound = decision["lower_bound"]
    schema_version = int(decision["schema_version"])

    if not sync_key:
        return [
            AgentBatchInfo(
                table_name=table_name,
                status="no_reliable_sync_key",
                message="No reliable sync key was available for incremental paging.",
                export_strategy=strategy,
                schema_version=schema_version,
                schema_fingerprint=table_schema.schema_fingerprint,
                uploaded=False,
                schema_changed=decision.get("schema_changed", False),
            )
        ]

    total_limit = max(1, int(total_limit))
    page_size = max(1, min(int(page_size), total_limit))
    candidate_rows = count_incremental_candidate_rows(
        db_path,
        table_name,
        sync_key=sync_key,
        strategy=strategy,
        last_value=lower_bound,
        timestamp_overlap_seconds=cfg.sync_defaults.timestamp_overlap_seconds,
    )
    expected_rows = min(candidate_rows, total_limit)

    if expected_rows <= 0:
        return [
            AgentBatchInfo(
                table_name=table_name,
                status="no_new_data",
                message="No rows were exported for this table.",
                export_strategy=strategy,
                schema_version=schema_version,
                schema_fingerprint=table_schema.schema_fingerprint,
                sync_key=sync_key,
                lower_bound=lower_bound,
                uploaded=False,
                schema_changed=decision.get("schema_changed", False),
            )
        ]

    total_parts = int(math.ceil(expected_rows / page_size))
    transfer_request_id = create_transfer_request_id(
        table_name=table_name,
        schema_version=schema_version,
        transfer_kind="inc",
    )

    batches: list[AgentBatchInfo] = []
    exported_rows = 0
    page_offset = 0

    for part_number in range(1, total_parts + 1):
        current_limit = min(page_size, expected_rows - exported_rows)
        df = read_incremental_rows_page(
            db_path,
            table_name,
            sync_key=sync_key,
            strategy=strategy,
            last_value=lower_bound,
            limit=current_limit,
            offset=page_offset,
            timestamp_overlap_seconds=cfg.sync_defaults.timestamp_overlap_seconds,
        )

        if df.empty:
            break

        file_stem = create_part_file_stem(
            table_name=table_name,
            schema_version=schema_version,
            query_short="inc",
            request_id=transfer_request_id,
            part_number=part_number,
            total_parts=total_parts,
        )
        part_extra = {
            "schema_changed": decision.get("schema_changed", False),
            "transfer_policy": "paged_incremental",
            "file_stem": file_stem,
            "transfer_request": {
                "request_id": transfer_request_id,
                "source_table": table_name,
                "query_type": decision["query_type"],
                "export_strategy": strategy,
                "part_number": part_number,
                "total_parts": total_parts,
                "expected_rows": expected_rows,
                "page_size": page_size,
            },
            "incremental_part": {
                "request_id": transfer_request_id,
                "part_number": part_number,
                "total_parts": total_parts,
                "page_offset": page_offset,
                "page_size": page_size,
                "request_total_limit": total_limit,
                "expected_rows": expected_rows,
                "candidate_rows": candidate_rows,
                "original_lower_bound": lower_bound,
            },
        }

        parquet_path, metadata, upper_value = write_raw_parquet_batch(
            df,
            cfg,
            table_schema=table_schema,
            schema_version=schema_version,
            query_type=decision["query_type"],
            export_strategy=strategy,
            sync_key=sync_key,
            lower_bound=lower_bound,
            database_fingerprint=database_fingerprint,
            extra=part_extra,
        )
        metadata_path = write_metadata_file(parquet_path, metadata)

        upload_response = None
        if upload:
            upload_response = upload_created_batch(cfg, parquet_path, metadata)

        rows_in_page = int(metadata.row_count or len(df))
        batches.append(
            AgentBatchInfo(
                table_name=table_name,
                status="created",
                message=(
                    f"Incremental part {part_number}/{total_parts} created with {rows_in_page} row(s)."
                    + (" Uploaded." if upload else " Not uploaded.")
                ),
                batch_id=metadata.batch_id,
                query_type=metadata.query_type,
                export_strategy=metadata.export_strategy,
                parquet_path=str(parquet_path),
                metadata_path=str(metadata_path),
                row_count=rows_in_page,
                schema_version=metadata.schema_version,
                schema_fingerprint=metadata.schema_fingerprint,
                sync_key=metadata.sync_key,
                lower_bound=metadata.lower_bound,
                upper_bound=upper_value,
                uploaded=upload,
                upload_response=upload_response,
                schema_changed=bool((metadata.extra or {}).get("schema_changed", False)),
                transfer_request_id=transfer_request_id,
                part_number=part_number,
                total_parts=total_parts,
                file_name=Path(parquet_path).name,
            )
        )

        exported_rows += rows_in_page
        page_offset += rows_in_page
        if rows_in_page < current_limit:
            break

    return batches

def create_and_maybe_upload_full_snapshot_batches(
    cfg: AgentConfig,
    *,
    db_path: Path,
    database_fingerprint: str,
    table_schema: Any,
    decision: dict[str, Any],
    row_count: int,
    snapshot_fingerprint: str,
    page_size: int,
    upload: bool,
) -> tuple[list[AgentBatchInfo], dict[str, Any] | None, Any]:
    table_name = table_schema.table_name
    schema_version = int(decision["schema_version"])
    sync_key = decision.get("sync_key")

    if row_count <= 0:
        return [
            AgentBatchInfo(
                table_name=table_name,
                status="empty_table",
                message="Table exists, but contains no rows.",
                query_type="full_table_snapshot",
                export_strategy="full_snapshot",
                row_count=0,
                schema_version=schema_version,
                schema_fingerprint=table_schema.schema_fingerprint,
                sync_key=sync_key,
                uploaded=False,
                full_snapshot_reason=decision.get("full_snapshot_reason"),
                schema_changed=decision.get("schema_changed", False),
                snapshot_fingerprint=snapshot_fingerprint,
            )
        ], None, None

    page_size = max(1, int(page_size))
    total_parts = int(math.ceil(row_count / page_size))
    snapshot_id = create_snapshot_id(cfg, table_name, schema_version)
    manifest_response = None

    if upload and cfg.large_file_transfer.enabled:
        manifest_response = start_snapshot_manifest(
            cfg,
            snapshot_id=snapshot_id,
            table_schema=table_schema,
            schema_version=schema_version,
            database_fingerprint=database_fingerprint,
            snapshot_fingerprint=snapshot_fingerprint,
            expected_parts=total_parts,
            expected_rows=row_count,
            query_type="full_table_snapshot",
            export_strategy="full_snapshot",
            full_snapshot_reason=decision.get("full_snapshot_reason"),
        )

    batches: list[AgentBatchInfo] = []
    snapshot_upper_value = get_max_value(db_path, table_name, sync_key) if sync_key else None

    for part_number in range(1, total_parts + 1):
        offset = (part_number - 1) * page_size
        df = read_full_table_page(db_path, table_name, limit=page_size, offset=offset)
        if df.empty:
            continue

        file_stem = create_part_file_stem(
            table_name=table_name,
            schema_version=schema_version,
            query_short="full",
            request_id=snapshot_id,
            part_number=part_number,
            total_parts=total_parts,
        )
        part_extra = {
            "schema_changed": decision.get("schema_changed", False),
            "transfer_policy": "paged_full_snapshot",
            "file_stem": file_stem,
            "transfer_request": {
                "request_id": snapshot_id,
                "source_table": table_name,
                "query_type": "full_table_snapshot",
                "export_strategy": "full_snapshot",
                "part_number": part_number,
                "total_parts": total_parts,
                "expected_rows": row_count,
                "page_size": page_size,
            },
            "snapshot_manifest": {
                "snapshot_id": snapshot_id,
                "part_number": part_number,
                "total_parts": total_parts,
                "page_offset": offset,
                "page_size": page_size,
                "total_rows_in_snapshot": row_count,
                "snapshot_fingerprint": snapshot_fingerprint,
                "requires_finalize": bool(upload),
            },
        }

        parquet_path, metadata, upper_value = write_raw_parquet_batch(
            df,
            cfg,
            table_schema=table_schema,
            schema_version=schema_version,
            query_type="full_table_snapshot",
            export_strategy="full_snapshot",
            sync_key=sync_key,
            lower_bound=offset,
            full_snapshot_reason=decision.get("full_snapshot_reason"),
            database_fingerprint=database_fingerprint,
            extra=part_extra,
        )
        metadata_path = write_metadata_file(parquet_path, metadata)

        upload_response = None
        if upload:
            upload_response = upload_created_batch(cfg, parquet_path, metadata)

        batches.append(
            AgentBatchInfo(
                table_name=table_name,
                status="created",
                message=(
                    f"Full snapshot part {part_number}/{total_parts} created."
                    + (" Uploaded." if upload else " Not uploaded.")
                ),
                batch_id=metadata.batch_id,
                query_type=metadata.query_type,
                export_strategy=metadata.export_strategy,
                parquet_path=str(parquet_path),
                metadata_path=str(metadata_path),
                row_count=metadata.row_count,
                schema_version=metadata.schema_version,
                schema_fingerprint=metadata.schema_fingerprint,
                sync_key=metadata.sync_key,
                lower_bound=metadata.lower_bound,
                upper_bound=upper_value,
                uploaded=upload,
                upload_response=upload_response,
                full_snapshot_reason=metadata.full_snapshot_reason,
                schema_changed=bool((metadata.extra or {}).get("schema_changed", False)),
                snapshot_fingerprint=snapshot_fingerprint,
                snapshot_id=snapshot_id,
                snapshot_part_number=part_number,
                snapshot_total_parts=total_parts,
                transfer_request_id=snapshot_id,
                part_number=part_number,
                total_parts=total_parts,
                file_name=Path(parquet_path).name,
            )
        )

    finalize_response = None
    if upload and cfg.large_file_transfer.enabled:
        finalize_response = finalize_snapshot_manifest(
            cfg,
            snapshot_id=snapshot_id,
            expected_parts=total_parts,
            expected_rows=row_count,
            snapshot_fingerprint=snapshot_fingerprint,
        )
        for batch in batches:
            batch.snapshot_finalized = True

    manifest_summary = {
        "snapshot_id": snapshot_id,
        "source_table": table_name,
        "schema_version": schema_version,
        "expected_parts": total_parts,
        "expected_rows": row_count,
        "snapshot_fingerprint": snapshot_fingerprint,
        "started": manifest_response,
        "finalized": finalize_response,
        "upload": upload,
    }
    return batches, manifest_summary, snapshot_upper_value


def execute_sync(cfg: AgentConfig, request: AgentSyncRequest) -> AgentSyncResponse:
    validate_request_target(cfg, request.factory_id, request.machine_id)
    state = load_state(cfg.state_file)
    # max_records_per_table is a total row cap for incremental requests.
    # It must not override the per-file page size. The file/part sizes come
    # from config.yaml under large_file_transfer.
    incremental_total_limit = request.max_records_per_table or cfg.batch_max_records
    incremental_page_size = (
        cfg.large_file_transfer.incremental_page_size
        if cfg.large_file_transfer.enabled
        else incremental_total_limit
    )
    full_snapshot_page_size = cfg.large_file_transfer.full_snapshot_page_size
    requested_tables = set(request.tables or [])
    force_recreate_tables = set(request.force_recreate_tables or [])
    batches: list[AgentBatchInfo] = []
    schema_events: list[dict[str, Any]] = []
    snapshot_manifests: list[dict[str, Any]] = []

    with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
        snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
        current_tables = set(snapshot.tables.keys())
        previous_tables = set((state.get("tables") or {}).keys())

        for missing in sorted(previous_tables - current_tables):
            mark_table_missing(state, missing)
            schema_events.append({"event": "table_missing_in_source", "table_name": missing})

        table_names = sorted(current_tables)
        if requested_tables:
            table_names = [t for t in table_names if t in requested_tables]

        for table_name in table_names:
            table_schema = snapshot.tables[table_name]
            table_state = get_table_state(state, table_name)
            decision = decide_table_export(
                cfg,
                table_name=table_name,
                table_schema=table_schema,
                table_state=table_state,
                force_full_snapshot=request.force_full_snapshot,
            )

            if decision.get("schema_changed"):
                add_event(
                    state,
                    "schema_changed",
                    table_name,
                    old_schema_fingerprint=table_state.get("schema_fingerprint"),
                    new_schema_fingerprint=table_schema.schema_fingerprint,
                    new_schema_version=decision["schema_version"],
                )
                schema_events.append(
                    {
                        "event": "schema_changed",
                        "table_name": table_name,
                        "schema_version": decision["schema_version"],
                        "schema_fingerprint": table_schema.schema_fingerprint,
                    }
                )

            if decision["export_strategy"] == "full_snapshot":
                row_count = get_table_row_count(readable_path, table_name)
                # Use the full table fingerprint, not the page size. This avoids
                # skipping a changed table just because the first page is identical.
                full_snapshot_fingerprint = get_table_content_fingerprint(
                    readable_path,
                    table_name,
                    table_schema,
                    limit=None,
                )
                allow_duplicate_skip = table_name not in force_recreate_tables
                if allow_duplicate_skip and can_skip_duplicate_full_snapshot(
                    table_state,
                    current_snapshot_fingerprint=full_snapshot_fingerprint,
                    upload=request.upload,
                ):
                    batches.append(
                        AgentBatchInfo(
                            table_name=table_name,
                            status="already_up_to_date",
                            message=(
                                "The table was checked and its content is identical to the last "
                                "successfully finalized full export. The Agent skipped this table "
                                "to prevent duplicate stored data."
                            ),
                            query_type="full_table_snapshot",
                            export_strategy="full_snapshot",
                            row_count=row_count,
                            schema_version=decision["schema_version"],
                            schema_fingerprint=table_schema.schema_fingerprint,
                            sync_key=decision.get("sync_key"),
                            uploaded=False,
                            full_snapshot_reason="duplicate_full_snapshot_skipped",
                            schema_changed=decision.get("schema_changed", False),
                            snapshot_fingerprint=full_snapshot_fingerprint,
                            previous_batch_id=table_state.get("last_full_snapshot_batch"),
                        )
                    )
                    continue

                table_batches, manifest_summary, snapshot_upper_value = create_and_maybe_upload_full_snapshot_batches(
                    cfg,
                    db_path=readable_path,
                    database_fingerprint=snapshot.database_fingerprint,
                    table_schema=table_schema,
                    decision=decision,
                    row_count=row_count,
                    snapshot_fingerprint=full_snapshot_fingerprint,
                    page_size=full_snapshot_page_size,
                    upload=request.upload,
                )
                batches.extend(table_batches)
                if manifest_summary:
                    snapshot_manifests.append(manifest_summary)

                created_any = any(batch.status == "created" for batch in table_batches)
                empty_table = any(batch.status == "empty_table" for batch in table_batches)
                if created_any or empty_table:
                    first_batch = next((b for b in table_batches if b.status == "created"), None)
                    snapshot_id = manifest_summary.get("snapshot_id") if manifest_summary else None
                    update_table_after_batch(
                        state,
                        table_name=table_name,
                        schema_fingerprint=table_schema.schema_fingerprint,
                        schema_version=decision["schema_version"],
                        sync_strategy="full_snapshot",
                        sync_key=decision.get("sync_key"),
                        last_value=snapshot_upper_value,
                        batch_id=snapshot_id or (first_batch.batch_id if first_batch else f"{table_name}_empty_snapshot"),
                        row_count=row_count,
                        row_count_at_last_snapshot=row_count,
                        parquet_path=first_batch.parquet_path if first_batch else None,
                        uploaded=request.upload,
                        full_snapshot_fingerprint=full_snapshot_fingerprint,
                    )
                continue

            table_batches = create_and_maybe_upload_incremental_batches(
                cfg,
                db_path=readable_path,
                database_fingerprint=snapshot.database_fingerprint,
                table_schema=table_schema,
                decision=decision,
                total_limit=incremental_total_limit,
                page_size=incremental_page_size,
                upload=request.upload,
            )
            batches.extend(table_batches)

            created_batches = [b for b in table_batches if b.status == "created"]
            if created_batches and all((not request.upload or b.upload_response is not None) for b in created_batches):
                last_batch = created_batches[-1]
                total_rows = sum(int(b.row_count or 0) for b in created_batches)
                update_table_after_batch(
                    state,
                    table_name=table_name,
                    schema_fingerprint=table_schema.schema_fingerprint,
                    schema_version=last_batch.schema_version or decision["schema_version"],
                    sync_strategy=last_batch.export_strategy or decision["export_strategy"],
                    sync_key=last_batch.sync_key,
                    last_value=last_batch.upper_bound,
                    batch_id=last_batch.batch_id or "unknown",
                    row_count=total_rows,
                    row_count_at_last_snapshot=None,
                    parquet_path=last_batch.parquet_path,
                    uploaded=last_batch.uploaded,
                    full_snapshot_fingerprint=None,
                )

        state["database_fingerprint"] = snapshot.database_fingerprint
        save_state(cfg.state_file, state)

    created = sum(1 for b in batches if b.status == "created")
    skipped = sum(1 for b in batches if b.status == "already_up_to_date")
    finalized = sum(1 for m in snapshot_manifests if m.get("finalized"))
    return AgentSyncResponse(
        status="completed",
        message=(
            f"Sync completed. {created} batch part(s) created. "
            f"{skipped} duplicate full snapshot(s) skipped. "
            f"{finalized} snapshot manifest(s) finalized."
        ),
        database_fingerprint=state.get("database_fingerprint"),
        batches=batches,
        schema_events=schema_events,
        snapshot_manifests=snapshot_manifests,
    )


def execute_limited_query(cfg: AgentConfig, request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    validate_request_target(cfg, request.factory_id, request.machine_id)
    with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
        snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
        if request.table not in snapshot.tables:
            raise HTTPException(status_code=404, detail=f"Table not found: {request.table}")

        table_schema = snapshot.tables[request.table]
        state = load_state(cfg.state_file)
        table_state = get_table_state(state, request.table)
        schema_version, schema_changed = create_schema_version(table_state, table_schema.schema_fingerprint)

        if schema_changed:
            add_event(
                state,
                "schema_changed",
                request.table,
                old_schema_fingerprint=table_state.get("schema_fingerprint"),
                new_schema_fingerprint=table_schema.schema_fingerprint,
                new_schema_version=schema_version,
            )

        try:
            df, query_info = read_limited_query(
                readable_path,
                schema=table_schema,
                columns=request.columns,
                where_column=request.where_column,
                operator=request.operator,
                value=request.value,
                filters=request.filters,
                time_column=request.time_column,
                start_time=request.start_time,
                end_time=request.end_time,
                limit=request.max_records,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if df.empty:
            batches = [
                AgentBatchInfo(
                    table_name=request.table,
                    status="no_matching_rows",
                    message="The custom query was valid, but no rows matched the selected filters.",
                    query_type="limited_query",
                    export_strategy="limited_query",
                    row_count=0,
                    schema_version=schema_version,
                    schema_fingerprint=table_schema.schema_fingerprint,
                    schema_changed=schema_changed,
                )
            ]
        else:
            parquet_path, metadata, upper_value = write_raw_parquet_batch(
                df,
                cfg,
                table_schema=table_schema,
                schema_version=schema_version,
                query_type="limited_query",
                export_strategy="limited_query",
                sync_key=None,
                database_fingerprint=snapshot.database_fingerprint,
                extra={
                    "schema_changed": schema_changed,
                    "limited_query": request.model_dump(mode="json", exclude_none=True),
                    "query_info": query_info,
                    "policy": "safe_single_table_parameterized_query",
                },
            )
            metadata_path = write_metadata_file(parquet_path, metadata)
            upload_response = upload_created_batch(cfg, parquet_path, metadata) if request.upload else None
            batches = [
                AgentBatchInfo(
                    table_name=request.table,
                    status="created",
                    message="Custom query batch created." + (" Uploaded." if request.upload else " Not uploaded."),
                    batch_id=metadata.batch_id,
                    query_type="limited_query",
                    export_strategy="limited_query",
                    parquet_path=str(parquet_path),
                    metadata_path=str(metadata_path),
                    row_count=metadata.row_count,
                    schema_version=schema_version,
                    schema_fingerprint=table_schema.schema_fingerprint,
                    upper_bound=upper_value,
                    uploaded=request.upload,
                    upload_response=upload_response,
                    schema_changed=schema_changed,
                )
            ]

        save_state(cfg.state_file, state)
        return AgentSyncResponse(
            status="completed",
            message="Custom query completed.",
            database_fingerprint=snapshot.database_fingerprint,
            batches=batches,
            schema_events=[]
            if not schema_changed
            else [
                {
                    "event": "schema_changed",
                    "table_name": request.table,
                    "schema_version": schema_version,
                    "schema_fingerprint": table_schema.schema_fingerprint,
                }
            ],
        )


def _repair_file_stem(original_metadata: RawBatchMetadata) -> str:
    """Use the original batch ID as the recreated filename stem."""
    return safe_file_part(original_metadata.batch_id, max_length=180)


def _repair_extra(original_metadata: RawBatchMetadata, item: AgentRepairItem) -> dict[str, Any]:
    extra = dict(original_metadata.extra or {})
    extra["file_stem"] = _repair_file_stem(original_metadata)
    extra["repair_request"] = {
        "is_repair": True,
        "original_batch_id": original_metadata.batch_id,
        "original_checksum": original_metadata.checksum_sha256,
        "original_storage_path": item.storage_path,
        "parquet_missing": item.parquet_missing,
        "metadata_missing": item.metadata_missing,
        "repair_created_at": datetime.now(timezone.utc).isoformat(),
        "note": "This batch was recreated because the Receiver metadata existed but one or more stored files were missing.",
    }
    return extra


def _read_repair_dataframe(
    *,
    db_path: Path,
    metadata: RawBatchMetadata,
    table_schema: Any,
    timestamp_overlap_seconds: int = 0,
) -> pd.DataFrame:
    extra = metadata.extra or {}
    row_count = max(1, int(metadata.row_count or 1))

    if metadata.query_type == "incremental":
        incremental_part = extra.get("incremental_part") or {}
        sync_key = metadata.sync_key
        if not sync_key:
            raise ValueError("Incremental repair requires sync_key in metadata.")
        page_offset = int(incremental_part.get("page_offset") or 0)
        lower_bound = incremental_part.get("original_lower_bound", metadata.lower_bound)
        return read_incremental_rows_page(
            db_path,
            metadata.source_table,
            sync_key=sync_key,
            strategy=metadata.export_strategy,
            last_value=lower_bound,
            limit=row_count,
            offset=page_offset,
            timestamp_overlap_seconds=timestamp_overlap_seconds,
        )

    if metadata.query_type == "full_table_snapshot":
        snapshot_manifest = extra.get("snapshot_manifest") or {}
        if snapshot_manifest:
            page_offset = int(snapshot_manifest.get("page_offset") or metadata.lower_bound or 0)
            return read_full_table_page(db_path, metadata.source_table, limit=row_count, offset=page_offset)
        # Legacy one-file full snapshot without manifest information.
        return read_full_table_page(db_path, metadata.source_table, limit=row_count, offset=0)

    if metadata.query_type == "limited_query":
        query_payload = extra.get("limited_query") or extra.get("custom_query")
        if not query_payload:
            raise ValueError(
                "This custom-query batch cannot be repaired automatically because the original query definition "
                "was not stored in metadata. Run the Custom Query again instead."
            )
        repair_request = AgentLimitedQueryRequest.model_validate(query_payload)
        try:
            df, _query_info = read_limited_query(
                db_path,
                schema=table_schema,
                columns=repair_request.columns,
                where_column=repair_request.where_column,
                operator=repair_request.operator,
                value=repair_request.value,
                filters=repair_request.filters,
                time_column=repair_request.time_column,
                start_time=repair_request.start_time,
                end_time=repair_request.end_time,
                limit=repair_request.max_records,
            )
        except ValueError as exc:
            raise ValueError(f"Stored custom-query definition is no longer valid for the current source schema: {exc}") from exc
        return df

    raise ValueError(
        f"Automatic repair is supported for incremental, full snapshot, and custom-query batches with stored query metadata. "
        f"Unsupported query_type: {metadata.query_type!r}"
    )


def repair_one_missing_batch(
    cfg: AgentConfig,
    *,
    db_path: Path,
    database_fingerprint: str,
    table_schema: Any,
    item: AgentRepairItem,
    upload: bool,
) -> AgentBatchInfo:
    original_metadata = RawBatchMetadata.model_validate_json(item.metadata_json)
    df = _read_repair_dataframe(
        db_path=db_path,
        metadata=original_metadata,
        table_schema=table_schema,
        timestamp_overlap_seconds=cfg.sync_defaults.timestamp_overlap_seconds,
    )

    if df.empty:
        return AgentBatchInfo(
            table_name=original_metadata.source_table,
            status="repair_skipped",
            message="The repair query returned no rows. The source database may have changed since the original transfer.",
            batch_id=original_metadata.batch_id,
            query_type=original_metadata.query_type,
            export_strategy=original_metadata.export_strategy,
            row_count=0,
            schema_version=original_metadata.schema_version,
            schema_fingerprint=original_metadata.schema_fingerprint,
            sync_key=original_metadata.sync_key,
            lower_bound=original_metadata.lower_bound,
            upper_bound=original_metadata.upper_bound,
            uploaded=False,
        )

    extra = _repair_extra(original_metadata, item)
    parquet_path, metadata, upper_value = write_raw_parquet_batch(
        df,
        cfg,
        table_schema=table_schema,
        schema_version=original_metadata.schema_version,
        query_type=original_metadata.query_type,
        export_strategy=original_metadata.export_strategy,
        sync_key=original_metadata.sync_key,
        lower_bound=original_metadata.lower_bound,
        full_snapshot_reason=original_metadata.full_snapshot_reason,
        database_fingerprint=database_fingerprint,
        extra=extra,
    )
    metadata_path = write_metadata_file(parquet_path, metadata)
    upload_response = upload_created_batch(cfg, parquet_path, metadata) if upload else None

    transfer_request = (metadata.extra or {}).get("transfer_request") or {}
    snapshot_manifest = (metadata.extra or {}).get("snapshot_manifest") or {}
    incremental_part = (metadata.extra or {}).get("incremental_part") or {}
    part_number = transfer_request.get("part_number") or snapshot_manifest.get("part_number") or incremental_part.get("part_number")
    total_parts = transfer_request.get("total_parts") or snapshot_manifest.get("total_parts") or incremental_part.get("total_parts")
    request_id = transfer_request.get("request_id") or snapshot_manifest.get("snapshot_id") or incremental_part.get("request_id") or metadata.batch_id

    return AgentBatchInfo(
        table_name=metadata.source_table,
        status="repaired",
        message="Missing Receiver file recreated from the current source database. Agent sync state was not changed.",
        batch_id=metadata.batch_id,
        query_type=metadata.query_type,
        export_strategy=metadata.export_strategy,
        parquet_path=str(parquet_path),
        metadata_path=str(metadata_path),
        row_count=metadata.row_count,
        schema_version=metadata.schema_version,
        schema_fingerprint=metadata.schema_fingerprint,
        sync_key=metadata.sync_key,
        lower_bound=metadata.lower_bound,
        upper_bound=upper_value,
        uploaded=upload,
        upload_response=upload_response,
        full_snapshot_reason=metadata.full_snapshot_reason,
        schema_changed=table_schema.schema_fingerprint != original_metadata.schema_fingerprint,
        snapshot_id=snapshot_manifest.get("snapshot_id"),
        snapshot_part_number=snapshot_manifest.get("part_number"),
        snapshot_total_parts=snapshot_manifest.get("total_parts"),
        transfer_request_id=request_id,
        part_number=part_number,
        total_parts=total_parts,
        file_name=Path(parquet_path).name,
    )


def execute_repair_missing_batches(cfg: AgentConfig, request: AgentRepairRequest) -> AgentSyncResponse:
    if not request.items:
        return AgentSyncResponse(status="completed", message="No repair items were provided.", batches=[])

    batches: list[AgentBatchInfo] = []
    schema_events: list[dict[str, Any]] = []

    with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
        snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
        for item in request.items:
            try:
                original_metadata = RawBatchMetadata.model_validate_json(item.metadata_json)
                table_name = original_metadata.source_table
                if table_name not in snapshot.tables:
                    batches.append(
                        AgentBatchInfo(
                            table_name=table_name,
                            status="repair_skipped",
                            message="Source table no longer exists in the factory database. This file can only be restored from a Receiver backup, or by taking a new source export if the table is created again.",
                            batch_id=original_metadata.batch_id,
                            query_type=original_metadata.query_type,
                            export_strategy=original_metadata.export_strategy,
                            row_count=0,
                            schema_version=original_metadata.schema_version,
                            schema_fingerprint=original_metadata.schema_fingerprint,
                            uploaded=False,
                        )
                    )
                    continue

                table_schema = snapshot.tables[table_name]
                batch = repair_one_missing_batch(
                    cfg,
                    db_path=readable_path,
                    database_fingerprint=snapshot.database_fingerprint,
                    table_schema=table_schema,
                    item=item,
                    upload=request.upload,
                )
                batches.append(batch)
                if batch.schema_changed:
                    schema_events.append(
                        {
                            "event": "repair_source_schema_differs_from_original_batch",
                            "table_name": table_name,
                            "schema_version": batch.schema_version,
                            "schema_fingerprint": table_schema.schema_fingerprint,
                        }
                    )
            except Exception as exc:  # Keep repairing other missing files.
                batches.append(
                    AgentBatchInfo(
                        table_name=item.source_table or "unknown",
                        status="repair_failed",
                        message=str(exc),
                        batch_id=item.batch_id,
                        uploaded=False,
                    )
                )

    repaired = sum(1 for b in batches if b.status == "repaired")
    failed = sum(1 for b in batches if b.status in {"repair_failed", "repair_skipped"})
    return AgentSyncResponse(
        status="completed",
        message=(
            f"Repair completed. {repaired} missing file(s) recreated. "
            f"{failed} item(s) could not be repaired automatically. Agent sync state was not changed."
        ),
        batches=batches,
        schema_events=schema_events,
    )


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/agent/auth/verify", tags=["auth"])
def verify_agent_auth(_: None = Depends(require_agent_api_key)) -> dict[str, Any]:
    return {
        "authenticated": True,
        "service": "factory-agent",
        "auth_scheme": "Bearer",
        "message": "Factory Agent bearer token is valid.",
    }


@app.get("/agent/config/summary", tags=["agent"], dependencies=[Depends(require_agent_api_key)])
def config_summary() -> dict[str, Any]:
    cfg = get_config()
    return {
        "factory_id": cfg.factory_id,
        "machine_id": cfg.machine_id,
        "sqlite_path": cfg.sqlite_path,
        "output_dir": cfg.output_dir,
        "state_file": cfg.state_file,
        "api_base_url": cfg.api_base_url,
        "environment": cfg.environment,
        "include_tables": cfg.include_tables,
        "exclude_tables": cfg.exclude_tables,
        "batch_max_records": cfg.batch_max_records,
        "use_snapshot": cfg.use_snapshot,
        "large_file_transfer": cfg.large_file_transfer.__dict__,
    }


@app.get("/agent/schema", tags=["agent"], dependencies=[Depends(require_agent_api_key)])
def schema_scan() -> dict[str, Any]:
    cfg = get_config()
    with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
        snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
    return snapshot.model_dump(mode="json")


@app.post(
    "/agent/sync/new-data",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def sync_new_data(request: AgentSyncRequest) -> AgentSyncResponse:
    cfg = get_config()
    request.force_full_snapshot = False
    return execute_sync(cfg, request)


@app.post(
    "/agent/sync/full-database",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def sync_full_database(request: AgentSyncRequest) -> AgentSyncResponse:
    cfg = get_config()
    request.force_full_snapshot = True
    return execute_sync(cfg, request)


@app.post(
    "/agent/repair/missing-batches",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def repair_missing_batches(request: AgentRepairRequest) -> AgentSyncResponse:
    cfg = get_config()
    return execute_repair_missing_batches(cfg, request)


@app.post(
    "/agent/query/limited",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def limited_query(request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    cfg = get_config()
    return execute_limited_query(cfg, request)


@app.post(
    "/agent/sync/limited-query",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def limited_query_sync_alias(request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    """Compatibility alias for Receiver UI limited-query requests."""
    cfg = get_config()
    return execute_limited_query(cfg, request)


@app.post(
    "/agent/limited-query",
    response_model=AgentSyncResponse,
    tags=["agent operations"],
    dependencies=[Depends(require_agent_api_key)],
)
def limited_query_short_alias(request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    """Compatibility alias for older local clients."""
    cfg = get_config()
    return execute_limited_query(cfg, request)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Factory Agent raw schema-aware replication runner.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mode", choices=["server", "incremental", "full-database", "schema"], default="server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--tables", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-upload", action="store_true")
    return parser


def run_server_mode(config_path: str, host: str, port: int, reload: bool) -> int:
    os.environ["AGENT_CONFIG"] = config_path
    import uvicorn

    uvicorn.run("agent.main:app", host=host, port=port, reload=reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "server":
        return run_server_mode(args.config, args.host, args.port, args.reload)

    os.environ["AGENT_CONFIG"] = args.config
    cfg = get_config()
    print_config_summary(cfg)

    if args.mode == "schema":
        with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
            snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
        print(snapshot.model_dump_json(indent=2))
        return 0

    request = AgentSyncRequest(
        factory_id=cfg.factory_id,
        machine_id=cfg.machine_id,
        tables=args.tables,
        max_records_per_table=args.limit,
        upload=not args.no_upload,
        force_full_snapshot=args.mode == "full-database",
    )
    response = execute_sync(cfg, request)
    print(response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
