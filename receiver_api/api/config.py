from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass
class ReceiverConfig:
    api_key: str
    storage_dir: str
    metadata_db_path: str
    factory_agent_base_url: str
    factory_agent_api_key: str
    environment: str = "local"


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


def _validate_http_url(name: str, value: str, *, environment: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be a valid HTTP URL, for example http://192.168.10.20:9000. Current value: {value!r}")
    if environment.lower() in {"prod", "production"}:
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError(
                f"{name} points to localhost while APP_ENV={environment!r}. "
                "For production HTTP deployment, use the real Factory Agent IP address or hostname."
            )


def load_config(path: str | Path = "config.yaml") -> ReceiverConfig:
    p = Path(path)
    if p.exists():
        raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    else:
        raw = {}

    environment = os.environ.get("APP_ENV", str(raw.get("environment", "local"))).strip() or "local"
    factory_agent_base_url = _env_or_raw(
        "FACTORY_AGENT_BASE_URL",
        raw,
        "factory_agent_base_url",
        "http://localhost:9000",
    )
    _validate_http_url("FACTORY_AGENT_BASE_URL/factory_agent_base_url", factory_agent_base_url, environment=environment)

    return ReceiverConfig(
        api_key=_required_env("RECEIVER_API_KEY"),
        storage_dir=str(os.environ.get("RECEIVER_STORAGE_DIR", raw.get("storage_dir", "storage/raw_parquet"))),
        metadata_db_path=str(os.environ.get("RECEIVER_METADATA_DB", raw.get("metadata_db_path", "storage/metadata.db"))),
        factory_agent_base_url=factory_agent_base_url,
        factory_agent_api_key=_required_env("FACTORY_AGENT_API_KEY"),
        environment=environment,
    )
