# Factory Agent

The Factory Agent runs on the factory computer that has access to the SQLite database. Its job is to read the database safely, prepare the requested data, convert it to transferable files, and upload those files to the Receiver API.

## What happens on the Factory Agent side

When the Receiver sends a request, the Factory Agent performs these steps:

```text
1. Load configuration
2. Authenticate the incoming request
3. Inspect the SQLite database schema
4. Decide which tables and rows must be exported
5. Read the required data from SQLite
6. Convert the result to Parquet
7. Create metadata for each file
8. Calculate SHA256 checksum
9. Upload the files to Receiver
10. Update local sync state if the transfer was successful
```

## Safe SQLite reading with snapshot

The Factory Agent can read from the SQLite database through a temporary snapshot.

```yaml
use_snapshot: true
```

When this is enabled, the Agent first creates a temporary copy of the SQLite database using SQLite backup functionality. The Agent then reads from this temporary copy instead of reading directly from the live database.

This is useful because the factory database may be used by another system at the same time. Reading from a snapshot helps to:

- reduce the risk of locking the live database,
- keep the read operation consistent,
- avoid interfering with the factory-side process,
- make long exports safer.

For local testing, `use_snapshot` can be disabled if needed. For production use, keeping it enabled is recommended.

## Export format

The Factory Agent exports data as Parquet files. Parquet is efficient for tabular data, usually smaller than CSV, and suitable for later processing with Python, pandas, PyArrow, Spark, or other data tools.

Each Parquet file is sent with metadata. The metadata describes:

- source database,
- source table,
- query type,
- export strategy,
- schema version,
- row count,
- checksum,
- file part number,
- transfer request ID,
- storage-related information.

## Large data handling

Large exports are split into multiple numbered part files.

Example:

```text
messungen_v3_inc_req_20260611_101500_ab12cd34_part_001_of_005.parquet
messungen_v3_inc_req_20260611_101500_ab12cd34_part_002_of_005.parquet
messungen_v3_inc_req_20260611_101500_ab12cd34_part_003_of_005.parquet
messungen_v3_inc_req_20260611_101500_ab12cd34_part_004_of_005.parquet
messungen_v3_inc_req_20260611_101500_ab12cd34_part_005_of_005.parquet
```

This makes it clear which files belong to the same request, which table they belong to, whether the transfer was incremental or full, and which part number each file has.

Important configuration values:

```yaml
batch_max_records: 100000

large_file_transfer:
  enabled: true
  full_snapshot_page_size: 50000
  incremental_page_size: 50000
```

| Setting | Purpose |
|---|---|
| `batch_max_records` | Maximum number of new rows per table in one incremental request. |
| `incremental_page_size` | Maximum number of rows inside each Parquet part for incremental exports. |
| `full_snapshot_page_size` | Maximum number of rows inside each Parquet part for full exports. |


If more new rows exist than `batch_max_records`, only up to that limit is sent in the current `Sync New Data` request. The remaining rows are sent in later sync requests.

## File verification

For every Parquet file, the Factory Agent calculates a SHA256 checksum. The checksum is included in the metadata and sent to the Receiver.

The Receiver calculates the checksum again after upload. If the checksum does not match, the file is rejected. The Factory Agent automatically retries the upload according to its retry settings, but if all retries fail, the transfer is not considered successful and the user must rerun the request or use the repair workflow. This prevents corrupted or incomplete files from being accepted.

## Sync state

The Factory Agent keeps a local state file:

```yaml
state_file: data/agent_state.json
```

This file stores information such as:

- last successful sync per table,
- last transferred ID or timestamp,
- schema version,
- previous full snapshot fingerprint,
- transfer events.

The state is used so `Sync New Data` does not resend rows that were already transferred.

Repair operations do not move the sync state forward. They only recreate missing files when possible.

## Main files

```text
factory_agent/
  agent/
    main.py
    config.py
    sqlite_reader.py
    exporter.py
    state.py
    uploader.py

  app_common/
    checksum.py
    schemas.py

  config.yaml
  pyproject.toml
```

| File | Responsibility |
|---|---|
| `agent/main.py` | FastAPI app, sync endpoints, full refresh, selected table sync, custom query, repair workflow, and main orchestration. |
| `agent/config.py` | Reads and validates `config.yaml` and environment variables. |
| `agent/sqlite_reader.py` | Reads SQLite, creates snapshots, inspects schemas, chooses sync strategies, and reads rows. |
| `agent/exporter.py` | Converts DataFrames to Parquet, creates readable file names, metadata, and part information. |
| `agent/state.py` | Stores local sync progress and schema state. |
| `agent/uploader.py` | Uploads Parquet files, metadata, manifests, and repair results to Receiver. |
| `app_common/checksum.py` | Calculates SHA256 checksums. |
| `app_common/schemas.py` | Shared metadata and schema models. |
| `config.yaml` | Runtime settings for database path, Receiver URL, transfer limits, snapshot use, and table selection. |
| `pyproject.toml` | Python project dependencies. |

## Prerequisites

Recommended:

- Python 3.11 or newer
- `uv`
- Access to the SQLite database file
- Network access from Factory Agent to Receiver API
- Port `9000` open if Receiver must call Factory Agent over the network

Install `uv` if needed:

```powershell
pip install uv
```

Install project dependencies:

```powershell
cd factory_agent
uv sync
```

## API keys

Two API keys are used:

| Key | Used by |
|---|---|
| `FACTORY_AGENT_API_KEY` | Receiver uses this token to call the Factory Agent. |
| `RECEIVER_API_KEY` | Factory Agent uses this token to upload files to Receiver. |

Create a strong key with Python:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set environment variables in PowerShell:

```powershell
$env:APP_ENV="local"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"
```

## Run locally for testing

```powershell
$env:APP_ENV="local"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd factory_agent
uv run python -m agent.main --mode server --config config.yaml --host 0.0.0.0 --port 9000
```

Use local mode for development, local database testing, and running Agent and Receiver on the same machine or local network.

## Run in production

```powershell
$env:APP_ENV="production"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd factory_agent
uv run python -m agent.main --mode server --config config.yaml --host 0.0.0.0 --port 9000
```

In production:

- set `sqlite_path` to the real factory SQLite database,
- set `api_base_url` to the Receiver API address,
- use strong API keys,
- keep `use_snapshot: true`,
- configure firewall rules,
- restrict access to port `9000` to trusted Receiver IP addresses.

## Firewall example for port 9000

Windows PowerShell as Administrator:

```powershell
New-NetFirewallRule `
  -DisplayName "Factory Agent API 9000" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 9000
```


