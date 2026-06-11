# Receiver API

The Receiver API runs on the receiving side. It provides the browser web application, sends requests to the Factory Agent, receives uploaded Parquet files, verifies them, stores them, and tracks metadata.

## Request methods in the web application

### Sync New Data

Use this for normal repeated transfers.

It sends only rows that were added after the last successful sync. This is the recommended daily or regular operation.

Use it when:

- the initial data has already been transferred,
- you only need new rows,
- you want to avoid resending old data.

If more new rows exist than the configured row limit, only part of them is sent in the current request. The rest will be sent in later `Sync New Data` requests.

### Full Database

Use this when you need a complete copy of all tables.

It requests the current content of every available table. Large tables are split into numbered part files. Unchanged full snapshots may be skipped to avoid duplicate storage.

Use it when:

- setting up the system for the first time,
- rebuilding a receiver storage area,
- checking the current full state of the factory database.

### Sync Selected Tables

Use this when only some tables need new data.

It works like `Sync New Data`, but only for the selected tables.

Use it when:

- only specific tables are relevant,
- you want to reduce transfer time,
- you want to test one table before syncing everything.

### Full Refresh Selected Tables

Use this when selected tables must be copied completely.

It requests the full current content of only the selected tables. Large tables are split into numbered part files.

Use it when:

- one table needs to be rebuilt,
- a table had a schema change,
- a table needs a fresh full snapshot.

### Custom Query

Use this to export a controlled subset of one table.

The UI allows selecting columns and filters. Free SQL is not sent directly. This keeps the query safer and easier to validate.

Use it when:

- you need a specific time range,
- you need specific columns,
- you need a smaller filtered dataset for analysis.

New Custom Query results store the query definition in metadata. This makes repair possible later if the file is deleted and the source table still exists.

### Inspect Schema

Use this to read only table and column information.

No row data is transferred.

Use it when:

- checking available tables,
- checking columns before creating a custom query,
- confirming whether a schema changed.

## Result output

After an operation, the UI shows information such as:

- operation status,
- table name,
- number of received rows,
- schema version,
- transfer strategy,
- number of part files,
- file names,
- storage paths,
- checksum values,
- repair or warning messages.

For large transfers, files are grouped by request ID and part number. This makes it clear which files belong to the same transfer request.

## Storage of received data

Receiver stores uploaded data as Parquet files.

Each Parquet file has a metadata JSON file next to it. Metadata is also registered in the local Receiver metadata database.

Typical storage structure:

```text
storage/raw_parquet/
  r/
    source_database/
      table_name/
        v1/
          table_v1_inc_req_..._part_001_of_005.parquet
          table_v1_inc_req_..._part_001_of_005.metadata.json

  q/
    source_database/
      table_name/
        v1/
          custom_query_file.parquet
          custom_query_file.metadata.json
```

| Folder | Purpose |
|---|---|
| `r` | Raw replication files, such as full and incremental transfers. |
| `q` | Custom Query result files. |

The Receiver metadata database stores:

- batch records,
- schema versions,
- storage paths,
- checksums,
- snapshot manifests,
- ignored missing batches.

## Reload status

`Reload status` checks whether files registered in Receiver metadata still exist on disk.

It can detect:

- missing Parquet files,
- missing metadata JSON files,
- missing full snapshot parts,
- files that were manually deleted.

If no files are missing, no warning is displayed.

## Handling missing files

If registered files are missing, the UI groups them by action.

### Repair can be tried

These files have enough metadata and the source table appears to exist. Receiver can ask the Factory Agent to recreate them.

Repair does not change the Factory Agent sync state.

### Run Custom Query again

This applies to older Custom Query files that were created before the full query definition was stored in metadata.

The recommended action is to run the same Custom Query again.

### Cannot repair automatically

These files cannot be recreated safely from the current source database. A common reason is that the source table no longer exists.

If the file is not needed anymore, the user can ignore it. Ignored missing files are stored in the Receiver metadata database and will not be shown again in future reload status checks.

## Main files

```text
receiver_api/
  api/
    main.py
    config.py
    db.py

  app_common/
    checksum.py
    schemas.py

  ui/
    index.html
    static/
      ui.js
      ui.css

  config.yaml
  pyproject.toml
```

| File | Responsibility |
|---|---|
| `api/main.py` | FastAPI app, UI routes, sync requests, upload handling, storage audit, repair actions, and ignore actions. |
| `api/config.py` | Reads Receiver settings such as storage path, metadata database path, Factory Agent URL, and timeout values. |
| `api/db.py` | Creates and updates the local metadata database. |
| `app_common/checksum.py` | Calculates SHA256 checksums for uploaded files. |
| `app_common/schemas.py` | Shared metadata and schema models. |
| `ui/index.html` | Browser UI structure. |
| `ui/static/ui.js` | UI logic, operation selection, progress display, result rendering, reload status, repair, and ignore actions. |
| `ui/static/ui.css` | Visual style for the UI. |
| `config.yaml` | Runtime configuration for Receiver. |
| `pyproject.toml` | Python project dependencies. |

## Prerequisites

Recommended:

- Python 3.11 or newer
- `uv`
- Network access to the Factory Agent
- Port `8000` available for the Receiver web application and API

Install `uv` if needed:

```powershell
pip install uv
```

Install project dependencies:

```powershell
cd receiver_api
uv sync
```

## API keys

Two API keys are used:

| Key | Used by |
|---|---|
| `RECEIVER_API_KEY` | Used to access Receiver UI/API and by Factory Agent when uploading files. |
| `FACTORY_AGENT_API_KEY` | Used by Receiver when calling the Factory Agent. |


Set environment variables in PowerShell:

```powershell
$env:APP_ENV="local"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"
```

The `RECEIVER_API_KEY` is the token you enter in the Receiver web UI.

## Run locally for testing

```powershell
$env:APP_ENV="local"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd receiver_api
uv run uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Run in production

```powershell
$env:APP_ENV="production"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd receiver_api
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000
```

In production:

- use strong API keys,
- set the Factory Agent URL correctly in `config.yaml`,
- protect Receiver access with network rules,
- expose port `8000` only to trusted users or networks,
- keep enough disk space for Parquet storage,
- back up the Receiver storage and metadata database.

Open:

```text
http://<system-ip-address>:8000
```

## Firewall example for port 8000

Windows PowerShell as Administrator:

```powershell
New-NetFirewallRule `
  -DisplayName "Receiver API 8000" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8000
```

