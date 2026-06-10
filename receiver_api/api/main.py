from __future__ import annotations

import hmac
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.config import ReceiverConfig, load_config
from api.db import (
    batch_exists,
    get_batch,
    init_db,
    insert_batch,
    list_all_batch_storage_records,
    list_batches,
    list_latest_full_snapshots,
    list_schemas,
)
from app_common.checksum import sha256_file
from app_common.schemas import RawBatchMetadata

_CONFIG_CACHE: ReceiverConfig | None = None
_CONFIG_CACHE_PATH: str | None = None
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
_UI_INDEX_PATH = _UI_DIR / "index.html"
_UI_STATIC_DIR = _UI_DIR / "static"

app = FastAPI(
    title="Receiver API",
    version="3.2.0",
    description="Receiver for schema-aware raw Parquet replication batches with a local UI.",
)

app.mount("/ui-static", StaticFiles(directory=str(_UI_STATIC_DIR)), name="ui_static")


class ReceiverSyncRequest(BaseModel):
    factory_id: str | None = None
    machine_id: str | None = None
    tables: list[str] | None = None
    max_records_per_table: int | None = Field(default=None, ge=1, le=5_000_000)
    upload: bool = True


class UiSyncRequest(BaseModel):
    mode: str = Field(
        default="new_data",
        description="new_data, full_database, selected_tables_new_data, selected_tables_full_snapshot, schema_only, or limited_query",
    )
    factory_id: str | None = None
    machine_id: str | None = None
    tables: list[str] | None = None
    max_records_per_table: int | None = Field(default=None, ge=1, le=5_000_000)

    # Safe custom query fields.
    table: str | None = None
    columns: list[str] | None = None
    where_column: str | None = None
    operator: str | None = None
    value: Any = None
    filters: list[dict[str, Any]] | None = None
    time_column: str | None = None
    start_time: Any = None
    end_time: Any = None
    max_records: int | None = Field(default=1000, ge=1, le=1_000_000)


class UploadResponse(BaseModel):
    status: str
    batch_id: str
    storage_path: str | None = None
    checksum_sha256: str | None = None
    row_count: int | None = None
    message: str | None = None


def safe_path_part(value: str) -> str:
    value = _SAFE_PATH_RE.sub("_", str(value).strip())
    return value or "unknown"


def get_config_path() -> str:
    return os.environ.get("RECEIVER_CONFIG", "config.yaml")


def get_config() -> ReceiverConfig:
    global _CONFIG_CACHE, _CONFIG_CACHE_PATH
    config_path = get_config_path()
    if _CONFIG_CACHE is None or _CONFIG_CACHE_PATH != config_path:
        _CONFIG_CACHE = load_config(config_path)
        _CONFIG_CACHE_PATH = config_path
        init_db(_CONFIG_CACHE.metadata_db_path)
    return _CONFIG_CACHE


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    cfg = get_config()
    token = extract_bearer_token(authorization)
    if token is None or not hmac.compare_digest(token, cfg.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def build_storage_dir(cfg: ReceiverConfig, metadata: RawBatchMetadata) -> Path:
    category = "q" if metadata.query_type == "limited_query" else "r"
    return (
        Path(cfg.storage_dir)
        / category
        / safe_path_part(metadata.factory_id)
        / safe_path_part(metadata.source_table)
        / f"v{metadata.schema_version}"
    )


@app.on_event("startup")
def startup() -> None:
    cfg = get_config()
    init_db(cfg.metadata_db_path)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/auth/verify", tags=["auth"])
def verify_auth(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return {
        "authenticated": True,
        "service": "receiver",
        "auth_scheme": "Bearer",
        "message": "Receiver bearer token is valid.",
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_UI_INDEX_PATH)


@app.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(_UI_INDEX_PATH)


@app.get("/api/v1/config/summary", tags=["receiver"], dependencies=[Depends(require_api_key)])
def config_summary() -> dict[str, Any]:
    cfg = get_config()
    return {
        "storage_dir": cfg.storage_dir,
        "metadata_db_path": cfg.metadata_db_path,
        "factory_agent_base_url": cfg.factory_agent_base_url,
        "environment": cfg.environment,
    }


@app.post("/api/v1/uploads/raw-batches", response_model=UploadResponse, tags=["uploads"], dependencies=[Depends(require_api_key)])
async def upload_raw_batch(
    file: UploadFile = File(...),
    metadata_json: str = Form(...),
) -> UploadResponse:
    cfg = get_config()
    metadata = RawBatchMetadata.model_validate_json(metadata_json)

    if batch_exists(cfg.metadata_db_path, metadata.batch_id):
        existing = get_batch(cfg.metadata_db_path, metadata.batch_id)
        existing_path = Path(existing["storage_path"]) if existing and existing.get("storage_path") else None
        existing_metadata_path = existing_path.with_suffix(".metadata.json") if existing_path else None
        if existing_path and existing_path.exists() and existing_metadata_path and existing_metadata_path.exists():
            return UploadResponse(
                status="duplicate_ignored",
                batch_id=metadata.batch_id,
                storage_path=str(existing_path),
                checksum_sha256=metadata.checksum_sha256,
                row_count=metadata.row_count,
                message="Batch was already stored and its files still exist. Duplicate upload ignored.",
            )
        # If the database row exists but files were manually deleted, this upload
        # cannot reuse the same primary key safely. The Agent normally creates a
        # new batch when Receiver reports missing files. This message explains the
        # inconsistent state clearly.
        raise HTTPException(
            status_code=409,
            detail=(
                "Batch metadata exists, but one or more stored files are missing. "
                "Run a new full refresh from the UI so the Agent creates a new batch."
            ),
        )

    storage_dir = build_storage_dir(cfg, metadata)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{safe_path_part(metadata.batch_id)}.parquet"

    with storage_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    actual_checksum = sha256_file(storage_path)
    if actual_checksum != metadata.checksum_sha256:
        storage_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail={
                "error": "checksum_mismatch",
                "expected": metadata.checksum_sha256,
                "actual": actual_checksum,
            },
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    insert_batch(cfg.metadata_db_path, metadata, str(storage_path), now_iso)

    metadata_copy = storage_path.with_suffix(".metadata.json")
    metadata_copy.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")

    return UploadResponse(
        status="uploaded",
        batch_id=metadata.batch_id,
        storage_path=str(storage_path),
        checksum_sha256=actual_checksum,
        row_count=metadata.row_count,
        message="Raw batch uploaded, checksum verified, and metadata registered.",
    )


@app.get("/api/v1/batches", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_batches(limit: int = 100) -> list[dict[str, Any]]:
    cfg = get_config()
    return list_batches(cfg.metadata_db_path, limit=limit)


@app.get("/api/v1/schemas", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_schemas(factory_id: str | None = None, source_table: str | None = None) -> list[dict[str, Any]]:
    cfg = get_config()
    return list_schemas(cfg.metadata_db_path, factory_id=factory_id, source_table=source_table)


def check_stored_batch_files(record: dict[str, Any]) -> dict[str, Any]:
    storage_path_text = record.get("storage_path")
    storage_path = Path(storage_path_text) if storage_path_text else None
    metadata_path = storage_path.with_suffix(".metadata.json") if storage_path else None

    parquet_exists = bool(storage_path and storage_path.exists())
    metadata_exists = bool(metadata_path and metadata_path.exists())
    files_complete = parquet_exists and metadata_exists

    return {
        **record,
        "parquet_exists": parquet_exists,
        "metadata_exists": metadata_exists,
        "files_complete": files_complete,
        "metadata_path": str(metadata_path) if metadata_path else None,
    }


def build_storage_audit(limit: int = 1000) -> dict[str, Any]:
    cfg = get_config()
    records = [check_stored_batch_files(r) for r in list_all_batch_storage_records(cfg.metadata_db_path, limit=limit)]
    missing_records = [r for r in records if not r["files_complete"]]

    latest_full = [check_stored_batch_files(r) for r in list_latest_full_snapshots(cfg.metadata_db_path)]
    missing_latest_full = [r for r in latest_full if not r["files_complete"]]

    complete_records = [r for r in records if r["files_complete"]]
    complete_tables = sorted({r["source_table"] for r in complete_records if r.get("source_table")})
    complete_raw_tables = sorted({
        r["source_table"]
        for r in complete_records
        if r.get("source_table") and r.get("query_type") != "limited_query"
    })

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage_root": cfg.storage_dir,
        "records_checked": len(records),
        "complete_file_records": len(complete_records),
        "complete_stored_tables_count": len(complete_raw_tables),
        "complete_stored_tables": complete_raw_tables,
        "complete_tables_including_query_results": complete_tables,
        "missing_file_records": len(missing_records),
        "latest_full_snapshots_checked": len(latest_full),
        "missing_latest_full_snapshots": len(missing_latest_full),
        "missing_latest_full_tables": sorted({r["source_table"] for r in missing_latest_full if r.get("source_table")}),
        "missing_records": missing_records[:100],
    }


@app.get("/api/v1/storage/audit", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_storage_audit(limit: int = 1000) -> dict[str, Any]:
    return build_storage_audit(limit=limit)


def force_recreate_tables_for_request(request: UiSyncRequest) -> list[str]:
    audit = build_storage_audit()
    missing_tables = set(audit.get("missing_latest_full_tables") or [])
    if request.mode == "selected_tables_full_snapshot":
        selected = set(request.tables or [])
        missing_tables = missing_tables.intersection(selected)
    elif request.mode != "full_database":
        missing_tables = set()
    return sorted(missing_tables)


def call_factory_agent(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    url = cfg.factory_agent_base_url.rstrip("/") + path
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg.factory_agent_api_key}"},
            json=payload,
            timeout=300,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Factory Agent request failed: {exc}") from exc


def call_factory_agent_with_fallback(paths: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    errors: list[str] = []
    for path in paths:
        url = cfg.factory_agent_base_url.rstrip("/") + path
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {cfg.factory_agent_api_key}"},
                json=payload,
                timeout=300,
            )
            if response.status_code == 404:
                errors.append(f"{path}: 404 Not Found")
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            errors.append(f"{path}: {exc}")
            continue
    raise HTTPException(
        status_code=502,
        detail=(
            "Factory Agent limited-query endpoint was not found or failed. "
            "Restart the Factory Agent after replacing factory_agent/agent/main.py. "
            f"Tried endpoints: {', '.join(paths)}. Errors: {' | '.join(errors)}"
        ),
    )


def get_factory_agent(path: str) -> dict[str, Any]:
    cfg = get_config()
    url = cfg.factory_agent_base_url.rstrip("/") + path
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {cfg.factory_agent_api_key}"},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Factory Agent request failed: {exc}") from exc


@app.post("/api/v1/factory-agent/sync/new-data", tags=["factory agent"])
def request_new_data(request: ReceiverSyncRequest, _: None = Depends(require_api_key)) -> dict[str, Any]:
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["upload"] = True
    return call_factory_agent("/agent/sync/new-data", payload)


@app.post("/api/v1/factory-agent/sync/full-database", tags=["factory agent"])
def request_full_database(request: ReceiverSyncRequest, _: None = Depends(require_api_key)) -> dict[str, Any]:
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["upload"] = True
    return call_factory_agent("/agent/sync/full-database", payload)


@app.post("/api/v1/factory-agent/query/limited", tags=["factory agent"])
def request_limited_query(request: UiSyncRequest, _: None = Depends(require_api_key)) -> dict[str, Any]:
    if not request.table:
        raise HTTPException(status_code=400, detail="A table is required for custom query mode.")
    if request.columns is not None and len(request.columns) == 0:
        raise HTTPException(status_code=400, detail="Select at least one column for the custom query.")

    filters = request.filters or []
    legacy_has_value_filter = request.value not in (None, "") or request.where_column
    if legacy_has_value_filter and not filters:
        if request.value not in (None, "") and not request.where_column:
            raise HTTPException(status_code=400, detail="Choose a value filter column or clear the filter value.")
        if request.where_column and request.value in (None, ""):
            raise HTTPException(status_code=400, detail="Enter a filter value or clear the value filter column.")

    cleaned_filters: list[dict[str, Any]] = []
    for item in filters:
        column = item.get("column")
        filter_type = item.get("filter_type") or item.get("type") or "value"
        if not column:
            # Empty UI filter rows are ignored only when they contain no values.
            if not any(item.get(k) not in (None, "") for k in ("value", "start_time", "end_time")):
                continue
            raise HTTPException(status_code=400, detail="Every custom filter must have a column.")

        if filter_type == "time" or item.get("start_time") not in (None, "") or item.get("end_time") not in (None, ""):
            start_time = item.get("start_time")
            end_time = item.get("end_time")
            if start_time in (None, "") and end_time in (None, ""):
                raise HTTPException(status_code=400, detail=f"Time filter for column {column} needs a start or end time.")
            cleaned: dict[str, Any] = {"column": column, "filter_type": "time"}
            if start_time not in (None, ""):
                cleaned["start_time"] = start_time
            if end_time not in (None, ""):
                cleaned["end_time"] = end_time
            cleaned_filters.append(cleaned)
            continue

        value = item.get("value")
        operator = item.get("operator") or "eq"
        if value in (None, ""):
            raise HTTPException(status_code=400, detail=f"Filter value for column {column} is empty.")
        cleaned_filters.append({"column": column, "operator": operator, "value": value})

    payload = request.model_dump(
        mode="json",
        include={
            "factory_id",
            "machine_id",
            "table",
            "columns",
            "where_column",
            "operator",
            "value",
            "filters",
            "max_records",
        },
        exclude_none=True,
    )
    if cleaned_filters:
        payload["filters"] = cleaned_filters
        payload.pop("where_column", None)
        payload.pop("operator", None)
        payload.pop("value", None)
    payload["upload"] = True
    return call_factory_agent_with_fallback(
        ["/agent/query/limited", "/agent/sync/limited-query", "/agent/limited-query"],
        payload,
    )


@app.get("/api/v1/factory-agent/schema", tags=["factory agent"], dependencies=[Depends(require_api_key)])
def request_schema() -> dict[str, Any]:
    return get_factory_agent("/agent/schema")


@app.get("/api/v1/connection/check", tags=["ui"], dependencies=[Depends(require_api_key)])
def connection_check() -> dict[str, Any]:
    cfg = get_config()
    result: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "receiver": {"status": "ok", "message": "Receiver API is running."},
        "factory_agent": {"status": "unknown", "message": "Not checked yet."},
        "schema_access": {"status": "unknown", "message": "Not checked yet."},
        "configuration": {
            "factory_agent_base_url": cfg.factory_agent_base_url,
            "storage_dir": cfg.storage_dir,
            "metadata_db_path": cfg.metadata_db_path,
        },
        "recommendations": [],
    }

    base = cfg.factory_agent_base_url.rstrip("/")
    try:
        health_response = requests.get(
            base + "/health",
            headers={"Authorization": f"Bearer {cfg.factory_agent_api_key}"},
            timeout=10,
        )
        if health_response.ok:
            result["factory_agent"] = {"status": "ok", "message": "Factory Agent health endpoint is reachable."}
        else:
            result["factory_agent"] = {
                "status": "error",
                "message": f"Factory Agent returned HTTP {health_response.status_code} from /health.",
            }
            result["recommendations"].append("Check that the Factory Agent is running on the configured host and port.")
    except requests.RequestException as exc:
        result["factory_agent"] = {"status": "error", "message": f"Cannot reach Factory Agent /health: {exc}"}
        result["recommendations"].append("Start the Factory Agent server and verify the configured factory_agent_base_url.")
        return result

    try:
        schema_response = requests.get(
            base + "/agent/schema",
            headers={"Authorization": f"Bearer {cfg.factory_agent_api_key}"},
            timeout=30,
        )
        if schema_response.ok:
            schema_json = schema_response.json()
            table_count = len(schema_json.get("tables") or {})
            result["schema_access"] = {
                "status": "ok",
                "message": f"Schema endpoint is reachable. Source tables found: {table_count}.",
                "source_tables": table_count,
            }
        else:
            result["schema_access"] = {
                "status": "error",
                "message": f"Factory Agent returned HTTP {schema_response.status_code} from /agent/schema: {schema_response.text[:500]}",
            }
            if schema_response.status_code in {401, 403}:
                result["recommendations"].append("Check that receiver_api/config.yaml contains the correct Factory Agent API key.")
            else:
                result["recommendations"].append("Check Factory Agent logs for schema inspection errors.")
    except requests.RequestException as exc:
        result["schema_access"] = {"status": "error", "message": f"Cannot read schema from Factory Agent: {exc}"}
        result["recommendations"].append("Check network connectivity and Factory Agent logs.")

    if not result["recommendations"]:
        result["recommendations"].append("Connection looks healthy. You can run a data request.")
    return result


@app.get("/api/v1/ui/options", tags=["ui"], dependencies=[Depends(require_api_key)])
def ui_options() -> dict[str, Any]:
    cfg = get_config()
    schema = get_factory_agent("/agent/schema")
    tables = []
    for name, table_schema in sorted((schema.get("tables") or {}).items()):
        columns = table_schema.get("columns") or []
        tables.append(
            {
                "name": name,
                "row_count": table_schema.get("row_count"),
                "schema_fingerprint": table_schema.get("schema_fingerprint"),
                "columns": [c.get("name") for c in columns],
                "column_details": columns,
            }
        )

    storage_audit = build_storage_audit()

    return {
        "receiver": {
            "storage_dir": cfg.storage_dir,
            "metadata_db_path": cfg.metadata_db_path,
            "factory_agent_base_url": cfg.factory_agent_base_url,
        },
        "storage_audit": storage_audit,
        "database_fingerprint": schema.get("database_fingerprint"),
        "tables": tables,
        "request_modes": [
            {
                "id": "new_data",
                "title": "Sync New Data",
                "description": "Recommended default. Transfers new rows when possible. First-time tables are fully copied once for safety.",
            },
            {
                "id": "full_database",
                "title": "Full Database",
                "description": "Copies the current database state table by table. Repeated unchanged copies are skipped to prevent duplicate stored batches.",
            },
            {
                "id": "selected_tables_new_data",
                "title": "Sync Selected Tables",
                "description": "Checks only the selected tables and transfers new rows where possible.",
            },
            {
                "id": "selected_tables_full_snapshot",
                "title": "Full Refresh Selected Tables",
                "description": "Copies the current state of selected tables. Use for recovery, validation, or known schema migrations. Repeated unchanged copies are skipped.",
            },
            {
                "id": "limited_query",
                "title": "Custom Query",
                "description": "Builds a controlled single-table query from selected columns, optional filters, and a row limit. Free-form SQL is not allowed.",
            },
            {
                "id": "schema_only",
                "title": "Inspect Schema",
                "description": "Reads table and column structure only.",
            },
        ],
        "query_options": {
            "supported_filters": ["table", "columns", "filters", "time_column", "start_time", "end_time", "max_records"],
            "operators": ["eq", "ne", "gt", "gte", "lt", "lte", "contains"],
            "safe_policy": "This UI does not execute free-form SQL. Custom queries are single-table, column-validated, parameterized requests.",
        },
    }


def explain_batch(batch: dict[str, Any]) -> dict[str, Any]:
    status = batch.get("status")
    table = batch.get("table_name")
    rows = batch.get("row_count") or 0
    query_type = batch.get("query_type")
    strategy = batch.get("export_strategy")
    reason = batch.get("full_snapshot_reason")
    upload_response = batch.get("upload_response") or {}
    storage_path = upload_response.get("storage_path") if isinstance(upload_response, dict) else None
    checksum = upload_response.get("checksum_sha256") if isinstance(upload_response, dict) else None

    if status == "created" and query_type == "limited_query":
        title = f"{table}: custom query stored"
        explanation = f"{rows} matching row(s) exported to a separate query batch."
    elif status == "already_up_to_date":
        title = f"{table}: already current"
        explanation = "Latest full copy is unchanged."
    elif status == "created" and query_type == "full_table_snapshot":
        if reason == "first_sync_or_new_table":
            title = f"{table}: initial full copy"
            explanation = f"Baseline created with {rows} row(s)."
        elif reason == "force_full_snapshot":
            title = f"{table}: full copy stored"
            explanation = f"Current table state stored with {rows} row(s)."
        elif reason == "previous_sync_key_missing_after_schema_change":
            title = f"{table}: full copy required"
            explanation = f"Sync key changed or was removed. Full copy stored with {rows} row(s)."
        elif reason == "no_reliable_incremental_key":
            title = f"{table}: full copy stored"
            explanation = f"No reliable incremental key was available. Stored {rows} row(s)."
        else:
            title = f"{table}: full copy stored"
            explanation = f"Stored {rows} row(s)."
    elif status == "created" and query_type == "incremental":
        title = f"{table}: new rows stored"
        explanation = f"Stored {rows} new row(s)."
    elif status == "no_new_data":
        title = f"{table}: no new rows"
        explanation = "Checked successfully. No new rows found."
    elif status == "no_matching_rows":
        title = f"{table}: no matching rows"
        explanation = "Custom query returned no rows."
    elif status == "empty_table":
        title = f"{table}: empty table"
        explanation = "Table exists, but contains no rows."
    else:
        title = f"{table}: {status}"
        explanation = batch.get("message") or "Status reported by the Factory Agent."

    if batch.get("schema_changed"):
        explanation += " Schema version updated."

    return {
        "table_name": table,
        "status": status,
        "title": title,
        "explanation": explanation,
        "row_count": rows,
        "query_type": query_type,
        "export_strategy": strategy,
        "schema_version": batch.get("schema_version"),
        "sync_key": batch.get("sync_key"),
        "storage_path": storage_path,
        "checksum_sha256": checksum,
        "previous_batch_id": batch.get("previous_batch_id"),
    }


def explain_sync_result(mode: str, raw_result: dict[str, Any], cfg: ReceiverConfig) -> dict[str, Any]:
    batches = raw_result.get("batches") or []
    schema_events = raw_result.get("schema_events") or []
    created = [b for b in batches if b.get("status") == "created"]
    skipped_duplicates = [b for b in batches if b.get("status") == "already_up_to_date"]
    no_new = [b for b in batches if b.get("status") in {"no_new_data", "no_matching_rows", "empty_table"}]
    transferred_rows = sum(int(b.get("row_count") or 0) for b in created)
    full_snapshots = [b for b in created if b.get("query_type") == "full_table_snapshot"]
    incremental = [b for b in created if b.get("query_type") == "incremental"]
    limited_queries = [b for b in created if b.get("query_type") == "limited_query"]

    if mode == "schema_only":
        headline = "Reads table and column structure only."
    elif created:
        headline = f"Completed. Stored {len(created)} batch(es) with {transferred_rows} row(s)."
    elif skipped_duplicates:
        headline = "Completed. Stored data is already current."
    else:
        headline = "Completed. No new rows found."

    return {
        "headline": headline,
        "storage_policy": "",
        "receiver_storage_root": cfg.storage_dir,
        "storage_audit": build_storage_audit(),
        "summary": {
            "created_batches": len(created),
            "transferred_rows": transferred_rows,
            "incremental_batches": len(incremental),
            "full_snapshot_batches": len(full_snapshots),
            "limited_query_batches": len(limited_queries),
            "tables_without_new_data": len(no_new),
            "duplicate_full_snapshots_skipped": len(skipped_duplicates),
            "schema_events": len(schema_events),
        },
        "schema_events": schema_events,
        "table_explanations": [explain_batch(b) for b in batches],
    }


def explain_schema_result(schema: dict[str, Any], cfg: ReceiverConfig) -> dict[str, Any]:
    tables = schema.get("tables") or {}
    table_items: list[dict[str, Any]] = []
    for table_name, table_schema in sorted(tables.items()):
        columns = table_schema.get("columns") or []
        column_names = [c.get("name") for c in columns if c.get("name")]
        table_items.append(
            {
                "table_name": table_name,
                "status": "schema_only",
                "title": table_name,
                "explanation": f"Columns found: {', '.join(column_names) if column_names else 'none'}",
                "row_count": 0,
                "query_type": "schema_scan",
                "export_strategy": None,
                "schema_version": None,
                "sync_key": None,
                "storage_path": None,
                "checksum_sha256": None,
                "columns": column_names,
            }
        )

    return {
        "schema_only": True,
        "headline": "Schema inspection",
        "storage_policy": "",
        "receiver_storage_root": None,
        "storage_audit": build_storage_audit(),
        "summary": {
            "created_batches": 0,
            "transferred_rows": 0,
            "incremental_batches": 0,
            "full_snapshot_batches": 0,
            "limited_query_batches": 0,
            "tables_without_new_data": 0,
            "duplicate_full_snapshots_skipped": 0,
            "schema_events": 0,
            "source_tables_found": len(table_items),
        },
        "schema_events": [],
        "table_explanations": table_items,
    }

@app.post("/api/v1/ui/sync", tags=["ui"])
def ui_sync(request: UiSyncRequest, _: None = Depends(require_api_key)) -> dict[str, Any]:
    cfg = get_config()

    if request.mode == "schema_only":
        schema = get_factory_agent("/agent/schema")
        return {
            "mode": request.mode,
            "raw_result": {"status": "completed", "message": "Schema inspected.", "batches": [], "schema_events": [], "schema": schema},
            "explanation": explain_schema_result(schema, cfg),
        }

    if request.mode == "limited_query":
        raw_result = request_limited_query(request, _)
        return {
            "mode": request.mode,
            "raw_result": raw_result,
            "explanation": explain_sync_result(request.mode, raw_result, cfg),
        }

    payload: dict[str, Any] = {
        "factory_id": request.factory_id,
        "machine_id": request.machine_id,
        "max_records_per_table": request.max_records_per_table,
        "upload": True,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    if request.mode == "new_data":
        raw_result = call_factory_agent("/agent/sync/new-data", payload)
    elif request.mode == "full_database":
        force_recreate = force_recreate_tables_for_request(request)
        if force_recreate:
            payload["force_recreate_tables"] = force_recreate
        raw_result = call_factory_agent("/agent/sync/full-database", payload)
    elif request.mode == "selected_tables_new_data":
        if not request.tables:
            raise HTTPException(status_code=400, detail="Select at least one table.")
        payload["tables"] = request.tables
        raw_result = call_factory_agent("/agent/sync/new-data", payload)
    elif request.mode == "selected_tables_full_snapshot":
        if not request.tables:
            raise HTTPException(status_code=400, detail="Select at least one table.")
        payload["tables"] = request.tables
        force_recreate = force_recreate_tables_for_request(request)
        if force_recreate:
            payload["force_recreate_tables"] = force_recreate
        raw_result = call_factory_agent("/agent/sync/full-database", payload)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported UI mode: {request.mode}")

    return {
        "mode": request.mode,
        "raw_result": raw_result,
        "explanation": explain_sync_result(request.mode, raw_result, cfg),
    }

