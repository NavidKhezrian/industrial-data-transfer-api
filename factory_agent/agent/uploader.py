from __future__ import annotations

import time
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
    timeout_seconds: int = 600,
    retries: int = 3,
    retry_backoff_seconds: float = 5.0,
) -> dict[str, Any]:
    """
    Upload one raw Parquet batch and its metadata to the Receiver API.

    The file handle is reopened for every retry. This is important because a
    failed multipart request may leave the stream position at EOF.
    """

    url = api_base_url.rstrip("/") + "/api/v1/uploads/raw-batches"
    path = Path(parquet_path)
    attempts = max(1, int(retries))
    last_error: requests.RequestException | None = None

    for attempt in range(1, attempts + 1):
        try:
            with path.open("rb") as f:
                response = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (path.name, f, "application/octet-stream")},
                    data={"metadata_json": metadata.model_dump_json()},
                    timeout=timeout_seconds,
                )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(max(0.0, float(retry_backoff_seconds)) * attempt)

    assert last_error is not None
    raise last_error
