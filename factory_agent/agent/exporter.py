from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from agent.config import AgentConfig
from app_common.checksum import sha256_file
from app_common.schemas import RawBatchMetadata, TableSchema


# Keep generated paths below this length on Windows.
# The project writes through file handles, but Windows installations can still fail
# when very long paths are passed to tools or later opened by users.
WINDOWS_SAFE_PATH_LIMIT = 240

_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_path_part(value: str, *, max_length: int = 160) -> str:
    """Convert a value into a safe folder/file path segment.

    The result stays readable, but it is deliberately capped so generated
    filenames do not exceed Windows path limits in deep installation folders.
    """
    text = _SAFE_PATH_RE.sub("_", str(value).strip()).strip("._-")
    if not text:
        text = "unknown"
    return text[:max_length]


def short_uuid(length: int = 12) -> str:
    """Return a short unique ID to keep Windows file paths short."""
    return uuid.uuid4().hex[:length]


def short_query_type(query_type: str) -> str:
    """Shorten query type for filenames."""
    mapping = {
        "full_table_snapshot": "full",
        "incremental": "inc",
        "limited_query": "query",
        "full_database_snapshot": "fulldb",
        "schema_only": "schema",
    }
    return mapping.get(query_type, safe_path_part(query_type, max_length=12))


def prepare_raw_dataframe(
    df: pd.DataFrame,
    *,
    factory_id: str,
    machine_id: str,
    table_name: str,
    batch_id: str,
) -> pd.DataFrame:
    """Add technical metadata columns to the exported raw dataframe.

    These columns do not interpret the factory data. They only make every row
    traceable to a batch, source, machine, and source table.
    """
    out = df.copy()
    exported_at = datetime.now(timezone.utc).isoformat()
    out.insert(0, "__batch_id", batch_id)
    out.insert(1, "__factory_id", factory_id)
    out.insert(2, "__machine_id", machine_id)
    out.insert(3, "__source_table", table_name)
    out.insert(4, "__exported_at", exported_at)
    return out


def max_value_from_dataframe(df: pd.DataFrame, sync_key: str | None) -> Any:
    """Return the maximum value of the sync key from the exported dataframe."""
    if not sync_key:
        return None
    if sync_key not in df.columns:
        return None
    if df.empty:
        return None

    series = df[sync_key].dropna()
    if series.empty:
        return None

    value = series.max()
    try:
        return value.item()
    except AttributeError:
        return value


def build_output_dir(
    cfg: AgentConfig,
    *,
    table_name: str,
    schema_version: int,
    query_type: str,
) -> Path:
    """Build a short stable local output directory for one table/schema version.

    Normal raw replication batches are stored under ``r``. Limited-query results
    are stored under ``q`` so ad-hoc query outputs never mix with normal
    replication history.
    """
    # Keep directory segments short. The previous implementation used the full
    # table/factory names in deep installation folders, which made full-snapshot
    # paths exceed Windows limits when a new table appeared.
    factory_id_safe = safe_path_part(cfg.factory_id, max_length=32)
    table_name_safe = safe_path_part(table_name, max_length=48)
    category = "q" if query_type == "limited_query" else "r"

    return (
        Path(cfg.output_dir).resolve()
        / category
        / factory_id_safe
        / table_name_safe
        / f"v{schema_version}"
    )


def ensure_directory_exists(directory: Path) -> None:
    """Create a directory using pathlib and os.makedirs."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.makedirs(str(directory), exist_ok=True)

    if not directory.exists():
        raise FileNotFoundError(f"Output directory was not created: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Output path exists but is not a directory: {directory}")


def verify_directory_is_writable(directory: Path) -> None:
    """Verify that the output directory is writable."""
    directory = Path(directory).resolve()
    ensure_directory_exists(directory)

    test_file = directory / f"w_{short_uuid()}.tmp"
    try:
        with open(test_file, "wb") as f:
            f.write(b"ok")
    finally:
        if test_file.exists():
            test_file.unlink()


def assert_path_not_too_long(path: Path, max_length: int = WINDOWS_SAFE_PATH_LIMIT) -> None:
    """Fail early with a clear error if the Windows path is too long."""
    path_text = str(Path(path).resolve())
    if os.name == "nt" and len(path_text) > max_length:
        raise OSError(
            "The output file path is too long for Windows. "
            f"Length: {len(path_text)}. "
            f"Path: {path_text}. "
            "Shorten cfg.output_dir in config.yaml, or use the built-in "
            "short filename generation in this exporter."
        )


def fit_file_stem_to_windows_path(
    directory: Path,
    file_stem: str,
    *,
    suffixes: tuple[str, ...] = (".parquet", ".metadata.json"),
    max_stem_length: int = 96,
    max_path_length: int = WINDOWS_SAFE_PATH_LIMIT,
) -> str:
    """Return a safe filename stem that fits inside the Windows path limit.

    Full-snapshot filenames can become long because the request ID, table name,
    schema version, and part number are all useful. This function keeps the name
    readable when possible, but trims it and appends a short unique token when
    the full path would exceed the configured Windows-safe path length.
    """
    stem = safe_path_part(file_stem, max_length=max_stem_length)

    if os.name != "nt":
        return stem

    directory_text = str(Path(directory).resolve())
    longest_suffix = max(len(suffix) for suffix in suffixes)

    # Directory + path separator + stem + longest suffix.
    available = max_path_length - len(directory_text) - 1 - longest_suffix

    if available >= len(stem):
        return stem

    if available < 24:
        raise OSError(
            "The output directory path is too long for Windows, even with a "
            "short generated filename. "
            f"Directory length: {len(directory_text)}. "
            f"Directory: {directory_text}. "
            "Set cfg.output_dir to a short path, for example E:/idt_batches."
        )

    token = short_uuid(8)
    prefix_length = max(8, available - len(token) - 1)
    prefix = stem[:prefix_length].rstrip("._-") or "batch"
    return f"{prefix}_{token}"


def write_dataframe_to_parquet_file_handle(
    df: pd.DataFrame,
    parquet_path: Path,
    *,
    compression: str,
) -> None:
    """Write Parquet through a binary file handle.

    This avoids pandas/pyarrow path handling issues and keeps the error clearer.
    """
    parquet_path = Path(parquet_path).resolve()
    ensure_directory_exists(parquet_path.parent)
    verify_directory_is_writable(parquet_path.parent)
    assert_path_not_too_long(parquet_path)

    with open(parquet_path, "wb") as file_handle:
        df.to_parquet(
            file_handle,
            index=False,
            compression=compression,
        )

    if not parquet_path.exists():
        raise FileNotFoundError(f"Parquet file was not created: {parquet_path}")
    if parquet_path.stat().st_size == 0:
        raise ValueError(f"Parquet file was created but is empty: {parquet_path}")


def build_batch_file_stem(
    *,
    cfg: AgentConfig,
    table_schema: TableSchema,
    schema_version: int,
    query_type: str,
    extra: dict[str, Any] | None,
) -> str:
    """Build the Parquet filename stem.

    Multipart transfers pass an explicit ``file_stem`` in metadata.extra. That
    explicit stem is now capped here and then checked again against the full
    Windows path in ``fit_file_stem_to_windows_path``.
    """
    extra = extra or {}
    explicit = extra.get("file_stem")
    if explicit:
        return safe_path_part(str(explicit), max_length=120)

    table_name_safe = safe_path_part(table_schema.table_name, max_length=48)
    factory_id_safe = safe_path_part(cfg.factory_id, max_length=32)
    query_short = short_query_type(query_type)
    uid = short_uuid(8)

    return safe_path_part(
        f"{factory_id_safe}_{table_name_safe}_v{schema_version}_{query_short}_{uid}",
        max_length=96,
    )


def write_raw_parquet_batch(
    df: pd.DataFrame,
    cfg: AgentConfig,
    *,
    table_schema: TableSchema,
    schema_version: int,
    query_type: str,
    export_strategy: str,
    sync_key: str | None,
    lower_bound: Any = None,
    upper_bound: Any = None,
    full_snapshot_reason: str | None = None,
    database_fingerprint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Path, RawBatchMetadata, Any]:
    """Write one raw table batch as a Parquet file and return its metadata."""
    output_dir = build_output_dir(
        cfg,
        table_name=table_schema.table_name,
        schema_version=schema_version,
        query_type=query_type,
    )
    ensure_directory_exists(output_dir)
    verify_directory_is_writable(output_dir)

    file_stem = build_batch_file_stem(
        cfg=cfg,
        table_schema=table_schema,
        schema_version=schema_version,
        query_type=query_type,
        extra=extra,
    )
    file_stem = fit_file_stem_to_windows_path(output_dir, file_stem)

    batch_id = file_stem
    parquet_path = (output_dir / f"{file_stem}.parquet").resolve()

    raw_out = prepare_raw_dataframe(
        df,
        factory_id=cfg.factory_id,
        machine_id=cfg.machine_id,
        table_name=table_schema.table_name,
        batch_id=batch_id,
    )

    write_dataframe_to_parquet_file_handle(
        raw_out,
        parquet_path,
        compression=cfg.compression,
    )

    checksum = sha256_file(parquet_path)
    upper_value = upper_bound if upper_bound is not None else max_value_from_dataframe(df, sync_key)

    metadata = RawBatchMetadata(
        factory_id=cfg.factory_id,
        machine_id=cfg.machine_id,
        source_database=str(cfg.sqlite_path),
        source_table=table_schema.table_name,
        batch_id=batch_id,
        query_type=query_type,
        export_strategy=export_strategy,
        schema_fingerprint=table_schema.schema_fingerprint,
        schema_version=schema_version,
        database_fingerprint=database_fingerprint,
        row_count=len(df),
        compression=cfg.compression,
        checksum_sha256=checksum,
        created_at=datetime.now(timezone.utc),
        sync_key=sync_key,
        lower_bound=lower_bound,
        upper_bound=upper_value,
        full_snapshot_reason=full_snapshot_reason,
        schema_snapshot=table_schema,
        extra=extra or {},
    )

    return parquet_path, metadata, upper_value


def write_metadata_file(parquet_path: Path, metadata: RawBatchMetadata) -> Path:
    """Write a JSON metadata file next to the Parquet file."""
    metadata_path = Path(parquet_path).resolve().with_suffix(".metadata.json")
    ensure_directory_exists(metadata_path.parent)
    verify_directory_is_writable(metadata_path.parent)
    assert_path_not_too_long(metadata_path)
    metadata_path.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
    return metadata_path
