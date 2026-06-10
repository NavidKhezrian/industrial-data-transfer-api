from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from app_common.schemas import RawBatchMetadata


def upload_batch(
    *,
    api_base_url: str,
    api_key: str,
    parquet_path: str | Path,
    metadata: RawBatchMetadata,
) -> dict[str, Any]:
    """Upload one raw Parquet batch and its metadata to Receiver API."""
    url = api_base_url.rstrip("/") + "/api/v1/uploads/raw-batches"
    path = Path(parquet_path)
    with path.open("rb") as f:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (path.name, f, "application/octet-stream")},
            data={"metadata_json": metadata.model_dump_json()},
            timeout=120,
        )
    response.raise_for_status()
    return response.json()
