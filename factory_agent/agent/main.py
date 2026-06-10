from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.config import AgentConfig, load_config
from agent.exporter import write_metadata_file, write_raw_parquet_batch
from agent.sqlite_reader import (
    choose_sync_strategy,
    get_table_content_fingerprint,
    get_table_row_count,
    inspect_database_schema,
    read_full_table,
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
    version="3.2.0",
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
    # Receiver-side safety override.
    # If the Receiver detects that previously stored files were manually deleted,
    # it sends the affected table names here. The Agent will not skip duplicate
    # full refreshes for these tables, even if its local state says the content
    # fingerprint is unchanged.
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


class AgentSyncResponse(BaseModel):
    status: str
    message: str
    database_fingerprint: str | None = None
    batches: list[AgentBatchInfo] = Field(default_factory=list)
    schema_events: list[dict[str, Any]] = Field(default_factory=list)


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


def upload_created_batch(cfg: AgentConfig, parquet_path: Path, metadata: RawBatchMetadata) -> dict[str, Any]:
    try:
        return upload_batch(
            api_base_url=cfg.api_base_url,
            api_key=cfg.receiver_api_key,
            parquet_path=parquet_path,
            metadata=metadata,
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


def create_and_maybe_upload_table_batch(
    cfg: AgentConfig,
    *,
    db_path: Path,
    database_fingerprint: str,
    table_schema: Any,
    decision: dict[str, Any],
    limit: int,
    upload: bool,
) -> AgentBatchInfo:
    table_name = table_schema.table_name
    strategy = decision["export_strategy"]
    sync_key = decision["sync_key"]
    lower_bound = decision["lower_bound"]

    if strategy == "full_snapshot":
        df = read_full_table(db_path, table_name, limit=limit)
    else:
        df = read_incremental_rows(
            db_path,
            table_name,
            sync_key=sync_key,
            strategy=strategy,
            last_value=lower_bound,
            limit=limit,
            timestamp_overlap_seconds=cfg.sync_defaults.timestamp_overlap_seconds,
        )

    if df.empty:
        return AgentBatchInfo(
            table_name=table_name,
            status="no_new_data" if decision["query_type"] == "incremental" else "empty_table",
            message="No rows were exported for this table.",
            export_strategy=strategy,
            schema_version=decision["schema_version"],
            schema_fingerprint=table_schema.schema_fingerprint,
            sync_key=sync_key,
            lower_bound=lower_bound,
            uploaded=False,
            full_snapshot_reason=decision.get("full_snapshot_reason"),
            schema_changed=decision.get("schema_changed", False),
        )

    parquet_path, metadata, upper_value = write_raw_parquet_batch(
        df,
        cfg,
        table_schema=table_schema,
        schema_version=decision["schema_version"],
        query_type=decision["query_type"],
        export_strategy=strategy,
        sync_key=sync_key,
        lower_bound=lower_bound,
        full_snapshot_reason=decision.get("full_snapshot_reason"),
        database_fingerprint=database_fingerprint,
        extra={"schema_changed": decision.get("schema_changed", False)},
    )
    metadata_path = write_metadata_file(parquet_path, metadata)

    upload_response = None
    if upload:
        upload_response = upload_created_batch(cfg, parquet_path, metadata)

    return AgentBatchInfo(
        table_name=table_name,
        status="created",
        message="Batch created." + (" Uploaded." if upload else " Not uploaded."),
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
    )


def execute_sync(cfg: AgentConfig, request: AgentSyncRequest) -> AgentSyncResponse:
    validate_request_target(cfg, request.factory_id, request.machine_id)
    state = load_state(cfg.state_file)
    limit = request.max_records_per_table or cfg.batch_max_records
    requested_tables = set(request.tables or [])
    force_recreate_tables = set(request.force_recreate_tables or [])

    batches: list[AgentBatchInfo] = []
    schema_events: list[dict[str, Any]] = []

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

            full_snapshot_fingerprint = None
            if decision["export_strategy"] == "full_snapshot":
                row_count = get_table_row_count(readable_path, table_name)
                full_snapshot_fingerprint = get_table_content_fingerprint(
                    readable_path,
                    table_name,
                    table_schema,
                    limit=limit,
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
                                "The table was checked and its content is identical to the last successfully stored full export. "
                                "The Agent skipped this table to prevent duplicate stored data."
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

            batch = create_and_maybe_upload_table_batch(
                cfg,
                db_path=readable_path,
                database_fingerprint=snapshot.database_fingerprint,
                table_schema=table_schema,
                decision=decision,
                limit=limit,
                upload=request.upload,
            )
            batch.snapshot_fingerprint = full_snapshot_fingerprint
            batches.append(batch)

            if batch.status == "created" and (not request.upload or batch.upload_response is not None):
                row_count_snapshot = None
                if batch.export_strategy == "full_snapshot":
                    row_count_snapshot = get_table_row_count(readable_path, table_name)
                update_table_after_batch(
                    state,
                    table_name=table_name,
                    schema_fingerprint=table_schema.schema_fingerprint,
                    schema_version=batch.schema_version or decision["schema_version"],
                    sync_strategy=batch.export_strategy or decision["export_strategy"],
                    sync_key=batch.sync_key,
                    last_value=batch.upper_bound,
                    batch_id=batch.batch_id or "unknown",
                    row_count=batch.row_count or 0,
                    row_count_at_last_snapshot=row_count_snapshot,
                    parquet_path=batch.parquet_path,
                    uploaded=batch.uploaded,
                    full_snapshot_fingerprint=full_snapshot_fingerprint,
                )

        state["database_fingerprint"] = snapshot.database_fingerprint
        save_state(cfg.state_file, state)

    created = sum(1 for b in batches if b.status == "created")
    skipped = sum(1 for b in batches if b.status == "already_up_to_date")
    return AgentSyncResponse(
        status="completed",
        message=f"Sync completed. {created} table batch(es) created. {skipped} duplicate full snapshot(s) skipped.",
        database_fingerprint=state.get("database_fingerprint"),
        batches=batches,
        schema_events=schema_events,
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
            schema_events=[] if not schema_changed else [
                {
                    "event": "schema_changed",
                    "table_name": request.table,
                    "schema_version": schema_version,
                    "schema_fingerprint": table_schema.schema_fingerprint,
                }
            ],
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
    }


@app.get("/agent/schema", tags=["agent"], dependencies=[Depends(require_agent_api_key)])
def schema_scan() -> dict[str, Any]:
    cfg = get_config()
    with readable_db(cfg.sqlite_path, cfg.use_snapshot) as readable_path:
        snapshot = inspect_database_schema(readable_path, cfg.include_tables, cfg.exclude_tables)
    return snapshot.model_dump(mode="json")


@app.post("/agent/sync/new-data", response_model=AgentSyncResponse, tags=["agent operations"], dependencies=[Depends(require_agent_api_key)])
def sync_new_data(request: AgentSyncRequest) -> AgentSyncResponse:
    cfg = get_config()
    request.force_full_snapshot = False
    return execute_sync(cfg, request)


@app.post("/agent/sync/full-database", response_model=AgentSyncResponse, tags=["agent operations"], dependencies=[Depends(require_agent_api_key)])
def sync_full_database(request: AgentSyncRequest) -> AgentSyncResponse:
    cfg = get_config()
    request.force_full_snapshot = True
    return execute_sync(cfg, request)


@app.post("/agent/query/limited", response_model=AgentSyncResponse, tags=["agent operations"], dependencies=[Depends(require_agent_api_key)])
def limited_query(request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    cfg = get_config()
    return execute_limited_query(cfg, request)


@app.post("/agent/sync/limited-query", response_model=AgentSyncResponse, tags=["agent operations"], dependencies=[Depends(require_agent_api_key)])
def limited_query_sync_alias(request: AgentLimitedQueryRequest) -> AgentSyncResponse:
    """Compatibility alias for Receiver UI limited-query requests."""
    cfg = get_config()
    return execute_limited_query(cfg, request)


@app.post("/agent/limited-query", response_model=AgentSyncResponse, tags=["agent operations"], dependencies=[Depends(require_agent_api_key)])
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
