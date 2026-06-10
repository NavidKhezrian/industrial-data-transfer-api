from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.config import ReceiverConfig, load_config
from api.db import (
    batch_exists,
    get_batch,
    get_snapshot_manifest,
    init_db,
    ignore_missing_batches,
    insert_batch,
    list_all_batch_storage_records,
    list_batches,
    list_ignored_missing_batch_ids,
    list_batches_for_snapshot,
    list_latest_full_snapshots,
    list_schemas,
    list_snapshot_manifests,
    update_snapshot_manifest_counts,
    upsert_batch,
    upsert_snapshot_manifest,
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
    version="3.4.0",
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
    file_name: str | None = None


class SnapshotManifestStartRequest(BaseModel):
    snapshot_id: str
    factory_id: str
    machine_id: str
    source_database: str | None = None
    source_table: str
    query_type: str = "full_table_snapshot"
    export_strategy: str = "full_snapshot"
    schema_fingerprint: str
    schema_version: int
    database_fingerprint: str | None = None
    snapshot_fingerprint: str
    expected_parts: int = Field(ge=1)
    expected_rows: int = Field(ge=0)
    full_snapshot_reason: str | None = None
    transfer_policy: str = "paged_full_snapshot_manifest"


class SnapshotManifestFinalizeRequest(BaseModel):
    snapshot_id: str
    expected_parts: int = Field(ge=1)
    expected_rows: int = Field(ge=0)
    snapshot_fingerprint: str


class SnapshotManifestResponse(BaseModel):
    status: str
    snapshot_id: str
    expected_parts: int
    received_parts: int
    expected_rows: int
    received_rows: int
    missing_parts: list[int] = Field(default_factory=list)
    message: str | None = None


class RepairMissingFilesResponse(BaseModel):
    status: str
    message: str
    repairable_missing_files: int
    repaired_files: int = 0
    exact_restores: int = 0
    recreated_from_current_source: int = 0
    failed_files: int = 0
    unrepaired_files: int = 0
    needs_rerun_files: int = 0
    not_repairable_files: int = 0
    raw_result: dict[str, Any] | None = None
    explanation: dict[str, Any] | None = None


class IgnoreMissingFilesResponse(BaseModel):
    status: str
    message: str
    ignored_files: int = 0
    ignored_batch_ids: list[str] = Field(default_factory=list)
    explanation: dict[str, Any] | None = None


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
        "factory_agent_request_timeout_seconds": cfg.factory_agent_request_timeout_seconds,
    }


def snapshot_info_from_metadata(metadata: RawBatchMetadata) -> dict[str, Any] | None:
    extra = metadata.extra or {}
    manifest = extra.get("snapshot_manifest")
    if isinstance(manifest, dict) and manifest.get("snapshot_id"):
        return manifest
    return None


def update_manifest_counts_from_uploaded_parts(snapshot_id: str) -> tuple[int, int]:
    cfg = get_config()
    rows = list_batches_for_snapshot(cfg.metadata_db_path, snapshot_id)
    received_parts = len(rows)
    received_rows = sum(int(row.get("row_count") or 0) for row in rows)
    update_snapshot_manifest_counts(
        cfg.metadata_db_path,
        snapshot_id,
        received_parts=received_parts,
        received_rows=received_rows,
        status=None,
        now_iso=datetime.now(timezone.utc).isoformat(),
    )
    return received_parts, received_rows


@app.post("/api/v1/uploads/snapshot-manifests/start", response_model=SnapshotManifestResponse, tags=["uploads"], dependencies=[Depends(require_api_key)])
def start_snapshot_manifest(request: SnapshotManifestStartRequest) -> SnapshotManifestResponse:
    cfg = get_config()
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = request.model_dump(mode="json")
    payload["status"] = "started"
    upsert_snapshot_manifest(cfg.metadata_db_path, payload, now_iso)
    received_parts, received_rows = update_manifest_counts_from_uploaded_parts(request.snapshot_id)
    return SnapshotManifestResponse(
        status="started",
        snapshot_id=request.snapshot_id,
        expected_parts=request.expected_parts,
        received_parts=received_parts,
        expected_rows=request.expected_rows,
        received_rows=received_rows,
        message="Snapshot manifest registered. Receiver is ready to receive parts.",
    )


@app.post("/api/v1/uploads/raw-batches", response_model=UploadResponse, tags=["uploads"], dependencies=[Depends(require_api_key)])
async def upload_raw_batch(
    file: UploadFile = File(...),
    metadata_json: str = Form(...),
) -> UploadResponse:
    cfg = get_config()
    metadata = RawBatchMetadata.model_validate_json(metadata_json)
    repair_info = (metadata.extra or {}).get("repair_request") or {}
    is_repair_upload = bool(repair_info.get("is_repair") or repair_info.get("original_batch_id"))

    existing = get_batch(cfg.metadata_db_path, metadata.batch_id) if batch_exists(cfg.metadata_db_path, metadata.batch_id) else None
    existing_path = Path(existing["storage_path"]) if existing and existing.get("storage_path") else None
    existing_metadata_path = existing_path.with_suffix(".metadata.json") if existing_path else None

    if existing and not is_repair_upload:
        if existing_path and existing_path.exists() and existing_metadata_path and existing_metadata_path.exists():
            return UploadResponse(
                status="duplicate_ignored",
                batch_id=metadata.batch_id,
                storage_path=str(existing_path),
                checksum_sha256=metadata.checksum_sha256,
                row_count=metadata.row_count,
                message="Batch was already stored and its files still exist. Duplicate upload ignored.",
                file_name=existing_path.name,
            )
        raise HTTPException(
            status_code=409,
            detail=(
                "Batch metadata exists, but one or more stored files are missing. "
                "Use the Storage repair action from the UI to recreate the missing file."
            ),
        )

    if existing and is_repair_upload and existing_path and existing_path.exists() and existing_metadata_path and existing_metadata_path.exists():
        return UploadResponse(
            status="repair_not_needed",
            batch_id=metadata.batch_id,
            storage_path=str(existing_path),
            checksum_sha256=existing.get("checksum_sha256") or metadata.checksum_sha256,
            row_count=existing.get("row_count") or metadata.row_count,
            message="Batch files already exist. Repair upload was not needed.",
            file_name=existing_path.name,
        )

    storage_dir = build_storage_dir(cfg, metadata)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = existing_path if existing_path else storage_dir / f"{safe_path_part(metadata.batch_id)}.parquet"
    storage_path.parent.mkdir(parents=True, exist_ok=True)

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
    if is_repair_upload:
        upsert_batch(cfg.metadata_db_path, metadata, str(storage_path), now_iso)
    else:
        insert_batch(cfg.metadata_db_path, metadata, str(storage_path), now_iso)

    metadata_copy = storage_path.with_suffix(".metadata.json")
    metadata_copy.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")

    snapshot_info = snapshot_info_from_metadata(metadata)
    if snapshot_info:
        manifest = get_snapshot_manifest(cfg.metadata_db_path, str(snapshot_info["snapshot_id"]))
        if manifest:
            update_manifest_counts_from_uploaded_parts(str(snapshot_info["snapshot_id"]))

    if is_repair_upload:
        original_checksum = str(repair_info.get("original_checksum") or "")
        exact_restore = bool(original_checksum and original_checksum == actual_checksum)
        status = "repaired_exact_match" if exact_restore else "repaired_from_current_source"
        message = (
            "Missing batch file restored. The checksum matches the original stored metadata."
            if exact_restore
            else "Missing batch file recreated from the current source database. The checksum differs from the original stored metadata."
        )
    else:
        status = "uploaded"
        message = "Raw batch uploaded, checksum verified, and metadata registered."

    return UploadResponse(
        status=status,
        batch_id=metadata.batch_id,
        storage_path=str(storage_path),
        checksum_sha256=actual_checksum,
        row_count=metadata.row_count,
        message=message,
        file_name=storage_path.name,
    )

def validate_snapshot_parts(snapshot_id: str) -> dict[str, Any]:
    cfg = get_config()
    manifest = get_snapshot_manifest(cfg.metadata_db_path, snapshot_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Snapshot manifest not found: {snapshot_id}")

    rows = list_batches_for_snapshot(cfg.metadata_db_path, snapshot_id)
    received_parts = len(rows)
    received_rows = sum(int(row.get("row_count") or 0) for row in rows)
    expected_parts = int(manifest["expected_parts"])
    expected_rows = int(manifest["expected_rows"])

    seen_parts: set[int] = set()
    missing_files: list[dict[str, Any]] = []
    for row in rows:
        info = row.get("snapshot_manifest") or {}
        part_number = int(info.get("part_number") or 0)
        if part_number:
            seen_parts.add(part_number)
        storage_path = Path(row["storage_path"])
        metadata_path = storage_path.with_suffix(".metadata.json")
        if not storage_path.exists() or not metadata_path.exists():
            missing_files.append(
                {
                    "batch_id": row.get("batch_id"),
                    "part_number": part_number,
                    "storage_path": str(storage_path),
                    "metadata_path": str(metadata_path),
                    "parquet_exists": storage_path.exists(),
                    "metadata_exists": metadata_path.exists(),
                }
            )

    missing_parts = [part for part in range(1, expected_parts + 1) if part not in seen_parts]
    return {
        "manifest": manifest,
        "rows": rows,
        "received_parts": received_parts,
        "received_rows": received_rows,
        "expected_parts": expected_parts,
        "expected_rows": expected_rows,
        "missing_parts": missing_parts,
        "missing_files": missing_files,
    }


@app.post("/api/v1/uploads/snapshot-manifests/finalize", response_model=SnapshotManifestResponse, tags=["uploads"], dependencies=[Depends(require_api_key)])
def finalize_snapshot_manifest(request: SnapshotManifestFinalizeRequest) -> SnapshotManifestResponse:
    cfg = get_config()
    validation = validate_snapshot_parts(request.snapshot_id)
    manifest = validation["manifest"]

    if manifest["snapshot_fingerprint"] != request.snapshot_fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "snapshot_fingerprint_mismatch",
                "expected": manifest["snapshot_fingerprint"],
                "actual": request.snapshot_fingerprint,
            },
        )

    if validation["expected_parts"] != request.expected_parts or validation["expected_rows"] != request.expected_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "manifest_expectation_mismatch",
                "manifest_expected_parts": validation["expected_parts"],
                "request_expected_parts": request.expected_parts,
                "manifest_expected_rows": validation["expected_rows"],
                "request_expected_rows": request.expected_rows,
            },
        )

    if validation["missing_parts"] or validation["missing_files"] or validation["received_rows"] != request.expected_rows:
        update_snapshot_manifest_counts(
            cfg.metadata_db_path,
            request.snapshot_id,
            received_parts=validation["received_parts"],
            received_rows=validation["received_rows"],
            status="incomplete",
            now_iso=datetime.now(timezone.utc).isoformat(),
            completed_at=None,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "snapshot_incomplete",
                "snapshot_id": request.snapshot_id,
                "expected_parts": request.expected_parts,
                "received_parts": validation["received_parts"],
                "missing_parts": validation["missing_parts"],
                "expected_rows": request.expected_rows,
                "received_rows": validation["received_rows"],
                "missing_files": validation["missing_files"],
            },
        )

    completed_at = datetime.now(timezone.utc).isoformat()
    update_snapshot_manifest_counts(
        cfg.metadata_db_path,
        request.snapshot_id,
        received_parts=validation["received_parts"],
        received_rows=validation["received_rows"],
        status="completed",
        now_iso=completed_at,
        completed_at=completed_at,
    )
    return SnapshotManifestResponse(
        status="completed",
        snapshot_id=request.snapshot_id,
        expected_parts=request.expected_parts,
        received_parts=validation["received_parts"],
        expected_rows=request.expected_rows,
        received_rows=validation["received_rows"],
        missing_parts=[],
        message="Snapshot manifest finalized. All parts are present and verified.",
    )


@app.get("/api/v1/batches", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_batches(limit: int = 100) -> list[dict[str, Any]]:
    cfg = get_config()
    return list_batches(cfg.metadata_db_path, limit=limit)


@app.get("/api/v1/schemas", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_schemas(factory_id: str | None = None, source_table: str | None = None) -> list[dict[str, Any]]:
    cfg = get_config()
    return list_schemas(cfg.metadata_db_path, factory_id=factory_id, source_table=source_table)


@app.get("/api/v1/snapshot-manifests", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_snapshot_manifests(limit: int = 200) -> list[dict[str, Any]]:
    cfg = get_config()
    return list_snapshot_manifests(cfg.metadata_db_path, limit=limit)


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


def manifest_audit_records() -> list[dict[str, Any]]:
    cfg = get_config()
    audited: list[dict[str, Any]] = []
    for manifest in list_snapshot_manifests(cfg.metadata_db_path, limit=500):
        snapshot_id = manifest["snapshot_id"]
        try:
            validation = validate_snapshot_parts(snapshot_id)
            missing_parts = validation["missing_parts"]
            missing_files = validation["missing_files"]
            files_complete = not missing_parts and not missing_files and validation["received_rows"] == validation["expected_rows"]
            audited.append(
                {
                    **manifest,
                    "files_complete": files_complete,
                    "received_parts_actual": validation["received_parts"],
                    "received_rows_actual": validation["received_rows"],
                    "missing_parts": missing_parts,
                    "missing_files": missing_files,
                }
            )
        except HTTPException:
            audited.append({**manifest, "files_complete": False, "missing_parts": [], "missing_files": []})
    return audited



def load_metadata_from_record(record: dict[str, Any]) -> RawBatchMetadata | None:
    metadata_json = record.get("metadata_json")
    if not metadata_json:
        return None
    try:
        return RawBatchMetadata.model_validate_json(metadata_json)
    except Exception:
        return None


def classify_missing_record(record: dict[str, Any], source_tables: set[str] | None = None) -> dict[str, Any]:
    """Classify a missing stored file for the UI and repair workflow.

    A local metadata record can tell us what file is missing. A live source
    schema, when available, lets us avoid calling a file truly repairable when
    its source table has already disappeared from the Factory database.
    """
    metadata = load_metadata_from_record(record)
    query_type = str(record.get("query_type") or "")
    export_strategy = str(record.get("export_strategy") or "")

    classified = dict(record)
    classified["repair_category"] = "not_repairable"
    classified["repair_action"] = "manual_review"
    classified["repair_reason"] = "This missing file type is not supported by automatic repair."

    if metadata is None:
        classified["repair_reason"] = "The Receiver metadata for this batch is missing or invalid."
        classified["repair_action"] = "manual_review"
        return classified

    source_table = metadata.source_table or record.get("source_table")
    source_was_checked = source_tables is not None
    source_table_exists = (source_table in source_tables) if source_was_checked else None

    if source_was_checked and not source_table_exists:
        classified["repair_category"] = "not_repairable"
        classified["repair_action"] = "restore_from_backup_or_new_full_export"
        classified["repair_reason"] = (
            "The Receiver still has a metadata record for this batch, but the source table is no longer "
            "present in the Factory database. It cannot be recreated from the current source database."
        )
        return classified

    extra = metadata.extra or {}
    if query_type in {"incremental", "full_table_snapshot"}:
        classified["repair_category"] = "repairable"
        classified["repair_action"] = "repair_from_source"
        if source_was_checked:
            classified["repair_reason"] = (
                "The source table currently exists in the Factory database. Repair can try to recreate "
                "this missing file from current source data without advancing the sync state."
            )
        else:
            classified["repair_reason"] = (
                "This batch type is usually repairable from the Factory database. The source table has not "
                "been checked in this local audit yet."
            )
        return classified

    if query_type == "limited_query":
        if extra.get("limited_query") or extra.get("custom_query"):
            classified["repair_category"] = "repairable"
            classified["repair_action"] = "repair_from_stored_query"
            classified["repair_reason"] = (
                "The original Custom Query definition is stored in metadata and the source table currently exists. "
                "Repair can try to recreate this query result from the current Factory database."
            )
            return classified
        classified["repair_category"] = "needs_rerun"
        classified["repair_action"] = "rerun_custom_query"
        classified["repair_reason"] = (
            "This is an old Custom Query result. The original query definition was not stored in metadata, "
            "so the system cannot safely recreate exactly the same file. Run the Custom Query again."
        )
        return classified

    if export_strategy == "limited_query":
        classified["repair_category"] = "needs_rerun"
        classified["repair_action"] = "rerun_custom_query"
        classified["repair_reason"] = "This appears to be a Custom Query result. Run the Custom Query again."
        return classified

    return classified


def build_storage_audit(limit: int = 1000, source_tables: set[str] | None = None) -> dict[str, Any]:
    cfg = get_config()
    ignored_batch_ids = list_ignored_missing_batch_ids(cfg.metadata_db_path)
    records = [check_stored_batch_files(r) for r in list_all_batch_storage_records(cfg.metadata_db_path, limit=limit)]
    all_missing_records = [classify_missing_record(r, source_tables=source_tables) for r in records if not r["files_complete"]]
    ignored_missing_records = [r for r in all_missing_records if str(r.get("batch_id") or "") in ignored_batch_ids]
    missing_records = [r for r in all_missing_records if str(r.get("batch_id") or "") not in ignored_batch_ids]
    repairable_missing_records = [r for r in missing_records if r.get("repair_category") == "repairable"]
    needs_rerun_missing_records = [r for r in missing_records if r.get("repair_category") == "needs_rerun"]
    not_repairable_missing_records = [r for r in missing_records if r.get("repair_category") == "not_repairable"]

    latest_full = [check_stored_batch_files(r) for r in list_latest_full_snapshots(cfg.metadata_db_path)]
    missing_latest_full = [r for r in latest_full if not r["files_complete"]]

    manifests = manifest_audit_records()
    completed_manifests = [m for m in manifests if m.get("status") == "completed"]
    incomplete_manifests = [m for m in manifests if m.get("status") != "completed"]
    completed_missing = [m for m in completed_manifests if not m.get("files_complete")]

    complete_records = [r for r in records if r["files_complete"]]
    complete_tables = sorted({r["source_table"] for r in complete_records if r.get("source_table")})
    complete_raw_tables = sorted(
        {
            r["source_table"]
            for r in complete_records
            if r.get("source_table") and r.get("query_type") != "limited_query"
        }
    )

    missing_manifest_tables = {m["source_table"] for m in completed_missing if m.get("source_table")}
    missing_latest_full_tables = sorted(
        {r["source_table"] for r in missing_latest_full if r.get("source_table")} | missing_manifest_tables
    )

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage_root": cfg.storage_dir,
        "source_schema_checked": source_tables is not None,
        "source_tables_seen": sorted(source_tables) if source_tables is not None else [],
        "records_checked": len(records),
        "complete_file_records": len(complete_records),
        "complete_stored_tables_count": len(complete_raw_tables),
        "complete_stored_tables": complete_raw_tables,
        "complete_tables_including_query_results": complete_tables,
        "missing_file_records": len(missing_records),
        "latest_full_snapshots_checked": len(latest_full),
        "missing_latest_full_snapshots": len(missing_latest_full),
        "missing_latest_full_tables": missing_latest_full_tables,
        "missing_records": missing_records[:100],
        "ignored_missing_records_count": len(ignored_missing_records),
        "ignored_missing_records": ignored_missing_records[:100],
        "repairable_missing_records_count": len(repairable_missing_records),
        "repairable_missing_records": repairable_missing_records[:100],
        "needs_rerun_missing_records_count": len(needs_rerun_missing_records),
        "needs_rerun_missing_records": needs_rerun_missing_records[:100],
        "not_repairable_missing_records_count": len(not_repairable_missing_records),
        "not_repairable_missing_records": not_repairable_missing_records[:100],
        "snapshot_manifests_checked": len(manifests),
        "completed_snapshot_manifests": len(completed_manifests),
        "incomplete_snapshot_manifests": len(incomplete_manifests),
        "completed_snapshot_manifests_with_missing_files": len(completed_missing),
        "incomplete_snapshot_tables": sorted({m["source_table"] for m in incomplete_manifests if m.get("source_table")}),
        "snapshot_manifest_issues": (completed_missing + incomplete_manifests)[:100],
    }


@app.get("/api/v1/storage/audit", tags=["metadata"], dependencies=[Depends(require_api_key)])
def get_storage_audit(limit: int = 1000) -> dict[str, Any]:
    return build_storage_audit(limit=limit)


def build_repair_candidates(limit: int = 1000, source_tables: set[str] | None = None) -> list[dict[str, Any]]:
    """Return metadata records whose Parquet file or sidecar metadata file is missing."""
    audit = build_storage_audit(limit=limit, source_tables=source_tables)
    candidates: list[dict[str, Any]] = []
    for record in audit.get("repairable_missing_records") or []:
        metadata_json = record.get("metadata_json")
        if not metadata_json:
            continue
        candidates.append(
            {
                "batch_id": record.get("batch_id"),
                "metadata_json": metadata_json,
                "storage_path": record.get("storage_path"),
                "metadata_path": record.get("metadata_path"),
                "source_table": record.get("source_table"),
                "query_type": record.get("query_type"),
                "export_strategy": record.get("export_strategy"),
                "schema_version": record.get("schema_version"),
                "row_count": record.get("row_count"),
                "parquet_missing": not bool(record.get("parquet_exists")),
                "metadata_missing": not bool(record.get("metadata_exists")),
                "repair_category": record.get("repair_category"),
                "repair_action": record.get("repair_action"),
                "repair_reason": record.get("repair_reason"),
            }
        )
    return candidates


def explain_repair_result(raw_result: dict[str, Any], cfg: ReceiverConfig) -> dict[str, Any]:
    batches = raw_result.get("batches") or []
    repaired = [b for b in batches if b.get("status") in {"repaired", "created"}]
    skipped = [b for b in batches if b.get("status") == "repair_skipped"]
    failed = [b for b in batches if b.get("status") not in {"repaired", "created", "repair_skipped"}]
    unrepaired = skipped + failed
    exact = 0
    changed = 0
    for batch in repaired:
        upload_response = batch.get("upload_response") or {}
        if upload_response.get("status") == "repaired_exact_match":
            exact += 1
        elif upload_response.get("status") == "repaired_from_current_source":
            changed += 1
    rows = sum(int(b.get("row_count") or 0) for b in repaired)
    transfer_groups = build_transfer_groups(batches)
    return {
        "headline": f"Repair finished. Restored {len(repaired)} missing file(s) with {rows} row(s). {len(unrepaired)} file(s) could not be repaired automatically.",
        "storage_policy": "Repair recreates missing files without advancing the Factory Agent incremental sync state.",
        "receiver_storage_root": cfg.storage_dir,
        "storage_audit": build_storage_audit(),
        "summary": {
            "created_batches": len(repaired),
            "transferred_rows": rows,
            "transfer_groups": len(transfer_groups),
            "part_files_received": len(repaired),
            "exact_restores": exact,
            "recreated_from_current_source": changed,
            "failed_files": len(failed),
            "skipped_files": len(skipped),
            "unrepaired_files": len(unrepaired),
        },
        "repair_summary": {
            "repaired_files": len(repaired),
            "exact_restores": exact,
            "recreated_from_current_source": changed,
            "failed_files": len(failed),
            "skipped_files": len(skipped),
            "unrepaired_files": len(unrepaired),
        },
        "schema_events": raw_result.get("schema_events") or [],
        "snapshot_manifests": raw_result.get("snapshot_manifests") or [],
        "transfer_groups": transfer_groups,
        "table_explanations": [explain_batch(b) for b in batches],
    }


@app.post("/api/v1/storage/ignore-not-repairable-missing-files", response_model=IgnoreMissingFilesResponse, tags=["metadata"], dependencies=[Depends(require_api_key)])
def ignore_not_repairable_missing_files(max_items: int = 1000) -> IgnoreMissingFilesResponse:
    """Hide current 'Cannot repair automatically' records from future storage warnings.

    This is intentionally separate from repair. It does not delete metadata, it
    does not recreate files, and it does not change the Factory Agent sync
    cursor. It only records that the user accepted these unresolved missing
    files and no longer wants to see them in Reload status warnings.
    """
    source_tables: set[str] | None = None
    try:
        schema = get_factory_agent("/agent/schema")
        source_tables = set((schema.get("tables") or {}).keys())
    except Exception:
        source_tables = None

    cfg = get_config()
    audit = build_storage_audit(limit=1000, source_tables=source_tables)
    records = audit.get("not_repairable_missing_records") or []
    batch_ids = [str(record.get("batch_id")) for record in records[: max(1, int(max_items))] if record.get("batch_id")]

    ignored_count = ignore_missing_batches(
        cfg.metadata_db_path,
        batch_ids,
        reason="User chose to ignore missing files that cannot be repaired automatically.",
        now_iso=datetime.now(timezone.utc).isoformat(),
    )
    refreshed_audit = build_storage_audit(limit=1000, source_tables=source_tables)

    if ignored_count:
        message = (
            f"Ignored {ignored_count} missing file record(s). They will no longer appear in Reload status warnings. "
            "The metadata is kept, and Sync New Data state is unchanged."
        )
        status = "ignored"
    else:
        message = "No 'Cannot repair automatically' missing files were found to ignore."
        status = "nothing_to_ignore"

    return IgnoreMissingFilesResponse(
        status=status,
        message=message,
        ignored_files=ignored_count,
        ignored_batch_ids=batch_ids[:ignored_count],
        explanation={
            "headline": message,
            "receiver_storage_root": cfg.storage_dir,
            "storage_policy": (
                "Ignore hides selected missing-file warnings only. It does not restore files, delete metadata, "
                "or move the Factory Agent sync cursor."
            ),
            "storage_audit": refreshed_audit,
            "summary": {
                "ignored_files": ignored_count,
                "remaining_missing_files": int(refreshed_audit.get("missing_file_records") or 0),
                "repairable_missing_files": int(refreshed_audit.get("repairable_missing_records_count") or 0),
                "needs_rerun_files": int(refreshed_audit.get("needs_rerun_missing_records_count") or 0),
                "not_repairable_files": int(refreshed_audit.get("not_repairable_missing_records_count") or 0),
            },
            "transfer_groups": [],
            "table_explanations": [],
            "schema_events": [],
        },
    )


@app.post("/api/v1/storage/repair-missing-files", response_model=RepairMissingFilesResponse, tags=["metadata"], dependencies=[Depends(require_api_key)])
def repair_missing_files(max_items: int = 100) -> RepairMissingFilesResponse:
    source_tables: set[str] | None = None
    try:
        schema = get_factory_agent("/agent/schema")
        source_tables = set((schema.get("tables") or {}).keys())
    except Exception:
        # Keep repair available even if the pre-check cannot read the schema.
        # The Factory Agent will still validate each item during the actual repair call.
        source_tables = None

    audit = build_storage_audit(limit=1000, source_tables=source_tables)
    candidates = build_repair_candidates(limit=1000, source_tables=source_tables)
    needs_rerun_count = int(audit.get("needs_rerun_missing_records_count") or 0)
    not_repairable_count = int(audit.get("not_repairable_missing_records_count") or 0)
    total_missing = int(audit.get("missing_file_records") or 0)

    if not candidates:
        if total_missing:
            message = "Missing files were found, but none of them are currently repairable from the Factory database. Check the listed reason for each file."
            status = "nothing_repairable"
        else:
            message = "No missing batch files were found in Receiver storage."
            status = "nothing_to_repair"
        return RepairMissingFilesResponse(
            status=status,
            message=message,
            repairable_missing_files=0,
            repaired_files=0,
            needs_rerun_files=needs_rerun_count,
            not_repairable_files=not_repairable_count,
            unrepaired_files=total_missing,
            explanation={
                "headline": message,
                "receiver_storage_root": get_config().storage_dir,
                "storage_policy": "Repair only tries files whose source table exists and whose metadata is sufficient. Custom Query results without stored query definitions must be rerun.",
                "storage_audit": audit,
                "summary": {
                    "created_batches": 0,
                    "transferred_rows": 0,
                    "part_files_received": 0,
                    "needs_rerun_files": needs_rerun_count,
                    "not_repairable_files": not_repairable_count,
                    "unrepaired_files": total_missing,
                },
                "transfer_groups": [],
                "table_explanations": [],
                "schema_events": [],
            },
        )

    selected = candidates[: max(1, int(max_items))]
    raw_result = call_factory_agent("/agent/repair/missing-batches", {"items": selected, "upload": True})
    cfg = get_config()
    explanation = explain_repair_result(raw_result, cfg)
    # Refresh audit after repair so the UI can show the current status, not the stale status.
    refreshed_audit = build_storage_audit(limit=1000, source_tables=source_tables)
    explanation["storage_audit"] = refreshed_audit
    repair_summary = explanation.get("repair_summary") or {}
    unrepaired_files = int(repair_summary.get("unrepaired_files") or 0)
    return RepairMissingFilesResponse(
        status="completed",
        message="Repair request finished. See explanation for restored files and warnings.",
        repairable_missing_files=len(candidates),
        repaired_files=int(repair_summary.get("repaired_files") or 0),
        exact_restores=int(repair_summary.get("exact_restores") or 0),
        recreated_from_current_source=int(repair_summary.get("recreated_from_current_source") or 0),
        failed_files=int(repair_summary.get("failed_files") or 0),
        unrepaired_files=unrepaired_files,
        needs_rerun_files=int(refreshed_audit.get("needs_rerun_missing_records_count") or 0),
        not_repairable_files=int(refreshed_audit.get("not_repairable_missing_records_count") or 0),
        raw_result=raw_result,
        explanation=explanation,
    )

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
            timeout=cfg.factory_agent_request_timeout_seconds,
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
                timeout=cfg.factory_agent_request_timeout_seconds,
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
            timeout=cfg.connection_check_timeout_seconds,
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
            timeout=cfg.connection_check_timeout_seconds,
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
            timeout=cfg.connection_check_timeout_seconds,
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

    source_table_names = set((schema.get("tables") or {}).keys())
    storage_audit = build_storage_audit(source_tables=source_table_names)
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
                "description": "",
            },
            {
                "id": "full_database",
                "title": "Full Database",
                "description": "",
            },
            {
                "id": "selected_tables_new_data",
                "title": "Sync Selected Tables",
                "description": "",
            },
            {
                "id": "selected_tables_full_snapshot",
                "title": "Full Refresh Selected Tables",
                "description": "",
            },
            {
                "id": "limited_query",
                "title": "Custom Query",
                "description": "",
            },
            {
                "id": "schema_only",
                "title": "Inspect Schema",
                "description": "",
            },
        ],
        "query_options": {
            "supported_filters": ["table", "columns", "filters", "time_column", "start_time", "end_time", "max_records"],
            "operators": ["eq", "ne", "gt", "gte", "lt", "lte", "contains"],
            "safe_policy": "This UI does not execute free-form SQL. Custom queries are single-table, column-validated, parameterized requests.",
        },
    }


def extract_part_info(batch: dict[str, Any]) -> dict[str, Any]:
    """Extract request/part metadata from the Agent response and upload response."""
    upload_response = batch.get("upload_response") or {}
    if not isinstance(upload_response, dict):
        upload_response = {}

    storage_path = upload_response.get("storage_path")
    checksum = upload_response.get("checksum_sha256")
    file_name = batch.get("file_name") or upload_response.get("file_name")
    if not file_name and storage_path:
        file_name = Path(str(storage_path)).name

    transfer_request_id = batch.get("transfer_request_id") or batch.get("snapshot_id") or batch.get("batch_id")
    part_number = batch.get("part_number") or batch.get("snapshot_part_number")
    total_parts = batch.get("total_parts") or batch.get("snapshot_total_parts")

    return {
        "transfer_request_id": transfer_request_id,
        "part_number": part_number,
        "total_parts": total_parts,
        "file_name": file_name,
        "storage_path": storage_path,
        "checksum_sha256": checksum,
    }


def explain_batch(batch: dict[str, Any]) -> dict[str, Any]:
    status = batch.get("status")
    table = batch.get("table_name")
    rows = batch.get("row_count") or 0
    query_type = batch.get("query_type")
    strategy = batch.get("export_strategy")
    reason = batch.get("full_snapshot_reason")
    part_info = extract_part_info(batch)
    storage_path = part_info["storage_path"]
    checksum = part_info["checksum_sha256"]
    part_number = part_info["part_number"]
    total_parts = part_info["total_parts"]

    if status == "created" and query_type == "limited_query":
        title = f"{table}: custom query stored"
        explanation = f"{rows} matching row(s) exported to a separate query batch."
    elif status == "already_up_to_date":
        title = f"{table}: already current"
        explanation = "Latest full copy is unchanged."
    elif status == "created" and query_type == "full_table_snapshot" and part_number and total_parts:
        title = f"{table}: full copy part {part_number}/{total_parts} stored"
        explanation = f"Stored {rows} row(s) in snapshot part {part_number}/{total_parts}."
        if batch.get("snapshot_finalized"):
            explanation += " Snapshot manifest finalized."
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
        if part_number and total_parts:
            title = f"{table}: new rows part {part_number}/{total_parts} stored"
            explanation = f"Stored {rows} new row(s) in part {part_number}/{total_parts}."
        else:
            title = f"{table}: new rows stored"
            explanation = f"Stored {rows} new row(s)."
    elif status == "repaired":
        upload_status = (batch.get("upload_response") or {}).get("status")
        if part_number and total_parts:
            title = f"{table}: repaired part {part_number}/{total_parts}"
        else:
            title = f"{table}: repaired missing file"
        if upload_status == "repaired_exact_match":
            explanation = f"Restored {rows} row(s). Checksum matches the original file."
        elif upload_status == "repaired_from_current_source":
            explanation = f"Recreated {rows} row(s) from the current source database. Checksum differs from the original file."
        else:
            explanation = f"Recreated missing file with {rows} row(s)."
    elif status == "repair_skipped":
        title = f"{table}: repair skipped"
        explanation = batch.get("message") or "This missing file could not be repaired automatically."
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
        "file_name": part_info["file_name"],
        "checksum_sha256": checksum,
        "previous_batch_id": batch.get("previous_batch_id"),
        "batch_id": batch.get("batch_id"),
        "snapshot_id": batch.get("snapshot_id"),
        "transfer_request_id": part_info["transfer_request_id"],
        "part_number": part_number,
        "total_parts": total_parts,
        "snapshot_part_number": batch.get("snapshot_part_number"),
        "snapshot_total_parts": batch.get("snapshot_total_parts"),
    }


def build_transfer_groups(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group created batch parts so the UI can show one request with all its files.
    """
    groups: dict[str, dict[str, Any]] = {}

    for batch in batches:
        if batch.get("status") != "created":
            continue
        part_info = extract_part_info(batch)
        request_id = part_info["transfer_request_id"] or batch.get("batch_id")
        if not request_id:
            continue

        key = str(request_id)
        group = groups.setdefault(
            key,
            {
                "transfer_request_id": key,
                "table_name": batch.get("table_name"),
                "query_type": batch.get("query_type"),
                "export_strategy": batch.get("export_strategy"),
                "schema_version": batch.get("schema_version"),
                "total_rows_received": 0,
                "received_parts": 0,
                "total_parts": part_info.get("total_parts") or 1,
                "parts": [],
            },
        )
        group["total_rows_received"] += int(batch.get("row_count") or 0)
        group["received_parts"] += 1
        if part_info.get("total_parts"):
            group["total_parts"] = max(int(group["total_parts"] or 0), int(part_info["total_parts"]))

        group["parts"].append(
            {
                "part_number": part_info.get("part_number") or group["received_parts"],
                "total_parts": part_info.get("total_parts") or group["total_parts"],
                "row_count": int(batch.get("row_count") or 0),
                "batch_id": batch.get("batch_id"),
                "file_name": part_info.get("file_name"),
                "storage_path": part_info.get("storage_path"),
                "checksum_sha256": part_info.get("checksum_sha256"),
            }
        )

    result = list(groups.values())
    for group in result:
        group["parts"].sort(key=lambda item: int(item.get("part_number") or 0))
    result.sort(key=lambda item: (str(item.get("table_name") or ""), str(item.get("transfer_request_id") or "")))
    return result


def explain_sync_result(mode: str, raw_result: dict[str, Any], cfg: ReceiverConfig) -> dict[str, Any]:
    batches = raw_result.get("batches") or []
    schema_events = raw_result.get("schema_events") or []
    snapshot_manifests = raw_result.get("snapshot_manifests") or []
    created = [b for b in batches if b.get("status") == "created"]
    skipped_duplicates = [b for b in batches if b.get("status") == "already_up_to_date"]
    no_new = [b for b in batches if b.get("status") in {"no_new_data", "no_matching_rows", "empty_table"}]
    transferred_rows = sum(int(b.get("row_count") or 0) for b in created)
    full_snapshots = [b for b in created if b.get("query_type") == "full_table_snapshot"]
    incremental = [b for b in created if b.get("query_type") == "incremental"]
    limited_queries = [b for b in created if b.get("query_type") == "limited_query"]
    finalized_manifests = [m for m in snapshot_manifests if m.get("finalized")]
    transfer_groups = build_transfer_groups(batches)
    total_parts = sum(int(group.get("received_parts") or 0) for group in transfer_groups)

    if mode == "schema_only":
        headline = "Reads table and column structure only."
    elif finalized_manifests:
        headline = (
            f"Completed. Finalized {len(finalized_manifests)} full snapshot(s), "
            f"received {transferred_rows} row(s) in {total_parts} part file(s)."
        )
    elif created:
        headline = f"Completed. Received {transferred_rows} row(s) in {total_parts or len(created)} part file(s)."
    elif skipped_duplicates:
        headline = "Completed. Stored data is already current."
    else:
        headline = "Completed. No new rows found."

    return {
        "headline": headline,
        "storage_policy": "Large transfers are stored as readable part files. Full snapshots are finalized only after all expected parts are received.",
        "receiver_storage_root": cfg.storage_dir,
        "storage_audit": build_storage_audit(),
        "summary": {
            "created_batches": len(created),
            "transferred_rows": transferred_rows,
            "transfer_groups": len(transfer_groups),
            "part_files_received": total_parts,
            "incremental_batches": len(incremental),
            "full_snapshot_batches": len(full_snapshots),
            "limited_query_batches": len(limited_queries),
            "finalized_snapshot_manifests": len(finalized_manifests),
            "tables_without_new_data": len(no_new),
            "duplicate_full_snapshots_skipped": len(skipped_duplicates),
            "schema_events": len(schema_events),
        },
        "schema_events": schema_events,
        "snapshot_manifests": snapshot_manifests,
        "transfer_groups": transfer_groups,
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
