from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

BatchQueryType = Literal[
    "incremental",
    "full_table_snapshot",
    "full_database_snapshot",
    "limited_query",
    "schema_scan",
]

TableStatus = Literal["active", "missing_in_source"]
SyncStrategy = Literal[
    "id_incremental",
    "updated_at_incremental",
    "timestamp_incremental",
    "full_snapshot",
    "limited_query",
]


class ColumnSchema(BaseModel):
    cid: int
    name: str
    type: str | None = None
    notnull: bool = False
    default_value: Any = None
    primary_key_position: int = 0


class ForeignKeySchema(BaseModel):
    id: int
    seq: int
    table: str
    from_column: str
    to_column: str | None = None
    on_update: str | None = None
    on_delete: str | None = None
    match: str | None = None


class TableSchema(BaseModel):
    table_name: str
    columns: list[ColumnSchema]
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)
    primary_key_columns: list[str] = Field(default_factory=list)
    schema_fingerprint: str


class DatabaseSchemaSnapshot(BaseModel):
    database_fingerprint: str
    scanned_at: datetime
    tables: dict[str, TableSchema]


class RawBatchMetadata(BaseModel):
    factory_id: str
    machine_id: str
    source_database: str
    source_table: str
    batch_id: str
    query_type: BatchQueryType
    export_strategy: SyncStrategy
    schema_fingerprint: str
    schema_version: int
    database_fingerprint: str | None = None
    row_count: int
    file_format: Literal["parquet"] = "parquet"
    compression: str = "zstd"
    checksum_sha256: str
    created_at: datetime
    uploaded_at: datetime | None = None
    upload_status: Literal["created", "uploaded", "failed"] = "created"
    sync_key: str | None = None
    lower_bound: Any = None
    upper_bound: Any = None
    full_snapshot_reason: str | None = None
    schema_snapshot: TableSchema | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# Backward-compatible alias for old imports.
BatchMetadata = RawBatchMetadata
