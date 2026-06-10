from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass
class SyncDefaults:
    id_candidates: list[str] = field(default_factory=lambda: ["id", "ID"])
    updated_at_candidates: list[str] = field(
        default_factory=lambda: ["updated_at", "modified_at", "last_modified", "changed_at"]
    )
    timestamp_candidates: list[str] = field(
        default_factory=lambda: ["timestamp", "created_at", "time", "datetime", "date"]
    )
    timestamp_overlap_seconds: int = 300
    full_snapshot_for_new_tables: bool = True
    full_snapshot_on_sync_key_loss: bool = True
    full_snapshot_on_database_reset: bool = True
    migration_table_change_ratio: float = 0.30


@dataclass
class LargeFileTransferConfig:
    """
    Settings for robust transfer of very large full-table snapshots.

    full_snapshot_page_size controls how many source rows are written into one
    Parquet file. A full table snapshot with more rows is exported as multiple
    parts and completed through a Receiver-side snapshot manifest.
    """

    enabled: bool = True
    full_snapshot_page_size: int = 100_000
    upload_timeout_seconds: int = 600
    upload_retries: int = 3
    upload_retry_backoff_seconds: float = 5.0
    manifest_timeout_seconds: int = 120
    finalize_timeout_seconds: int = 120
    incremental_page_size: int = 100_000


@dataclass
class AgentConfig:
    # Internal source identifiers are intentionally not exposed in config.
    # This deployment uses one SQLite database source, so stable internal values
    # are enough for metadata and receiver-side storage grouping.
    sqlite_path: str
    output_dir: str
    state_file: str
    api_base_url: str
    api_key: str
    receiver_api_key: str
    environment: str = "local"
    batch_max_records: int = 100_000
    compression: str = "zstd"
    use_snapshot: bool = True
    include_tables: list[str] = field(default_factory=lambda: ["*"])
    exclude_tables: list[str] = field(default_factory=lambda: ["sqlite_sequence"])
    sync_defaults: SyncDefaults = field(default_factory=SyncDefaults)
    large_file_transfer: LargeFileTransferConfig = field(default_factory=LargeFileTransferConfig)

    @property
    def factory_id(self) -> str:
        """Stable internal source ID used by Receiver metadata."""
        return "source_database"

    @property
    def machine_id(self) -> str:
        """Stable internal database ID used by Receiver metadata."""
        return "sqlite_database"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Generate a secure token and set it before starting the service."
        )
    return value


def _env_or_raw(env_name: str, raw: dict[str, Any], raw_key: str, default: str | None = None) -> str:
    value = os.environ.get(env_name)
    if value is not None and value.strip():
        return value.strip()
    if raw_key in raw and str(raw[raw_key]).strip():
        return str(raw[raw_key]).strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing required configuration value: {raw_key} or environment variable {env_name}")


def _as_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_http_url(name: str, value: str, *, environment: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            f"{name} must be a valid HTTP URL, for example http://192.168.10.50:8000. "
            f"Current value: {value!r}"
        )
    if environment.lower() in {"prod", "production"}:
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError(
                f"{name} points to localhost while APP_ENV={environment!r}. "
                "For production HTTP deployment, use the real Receiver IP address or hostname."
            )


def _validate_existing_sqlite_path(path: str) -> None:
    if not Path(path).exists():
        raise RuntimeError(
            f"SQLite database not found: {path}. "
            "Set sqlite_path in the config file or FACTORY_SQLITE_PATH in the environment."
        )


def load_config(path: str | Path) -> AgentConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    environment = os.environ.get("APP_ENV", str(raw.get("environment", "local"))).strip() or "local"

    sync_raw = raw.get("sync_defaults") or {}
    sync_defaults = SyncDefaults(
        id_candidates=_as_list(sync_raw.get("id_candidates"), ["id", "ID"]),
        updated_at_candidates=_as_list(
            sync_raw.get("updated_at_candidates"),
            ["updated_at", "modified_at", "last_modified", "changed_at"],
        ),
        timestamp_candidates=_as_list(
            sync_raw.get("timestamp_candidates"),
            ["timestamp", "created_at", "time", "datetime", "date"],
        ),
        timestamp_overlap_seconds=int(sync_raw.get("timestamp_overlap_seconds", 300)),
        full_snapshot_for_new_tables=_as_bool(sync_raw.get("full_snapshot_for_new_tables"), True),
        full_snapshot_on_sync_key_loss=_as_bool(sync_raw.get("full_snapshot_on_sync_key_loss"), True),
        full_snapshot_on_database_reset=_as_bool(sync_raw.get("full_snapshot_on_database_reset"), True),
        migration_table_change_ratio=float(sync_raw.get("migration_table_change_ratio", 0.30)),
    )

    large_raw = raw.get("large_file_transfer") or {}
    large_file_transfer = LargeFileTransferConfig(
        enabled=_as_bool(large_raw.get("enabled"), True),
        full_snapshot_page_size=int(large_raw.get("full_snapshot_page_size", raw.get("batch_max_records", 100_000))),
        upload_timeout_seconds=int(large_raw.get("upload_timeout_seconds", 600)),
        upload_retries=int(large_raw.get("upload_retries", 3)),
        upload_retry_backoff_seconds=float(large_raw.get("upload_retry_backoff_seconds", 5.0)),
        manifest_timeout_seconds=int(large_raw.get("manifest_timeout_seconds", 120)),
        finalize_timeout_seconds=int(large_raw.get("finalize_timeout_seconds", 120)),
        incremental_page_size=int(large_raw.get("incremental_page_size", raw.get("batch_max_records", 100_000))),
    )

    sqlite_path = os.environ.get("FACTORY_SQLITE_PATH", str(raw["sqlite_path"])).strip()
    api_base_url = _env_or_raw("RECEIVER_BASE_URL", raw, "api_base_url", "http://localhost:8000")
    _validate_http_url("RECEIVER_BASE_URL/api_base_url", api_base_url, environment=environment)
    _validate_existing_sqlite_path(sqlite_path)

    return AgentConfig(
        sqlite_path=sqlite_path,
        output_dir=str(os.environ.get("AGENT_OUTPUT_DIR", raw.get("output_dir", "data/agent_batches"))),
        state_file=str(os.environ.get("AGENT_STATE_FILE", raw.get("state_file", "data/agent_state.json"))),
        api_base_url=api_base_url,
        api_key=_required_env("FACTORY_AGENT_API_KEY"),
        receiver_api_key=_required_env("RECEIVER_API_KEY"),
        environment=environment,
        batch_max_records=int(os.environ.get("AGENT_BATCH_MAX_RECORDS", raw.get("batch_max_records", 100_000))),
        compression=str(os.environ.get("AGENT_COMPRESSION", raw.get("compression", "zstd"))),
        use_snapshot=_as_bool(raw.get("use_snapshot"), True),
        include_tables=_as_list(raw.get("include_tables"), ["*"]),
        exclude_tables=_as_list(raw.get("exclude_tables"), ["sqlite_sequence"]),
        sync_defaults=sync_defaults,
        large_file_transfer=large_file_transfer,
    )
