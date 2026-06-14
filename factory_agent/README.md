# Factory Agent

The Factory Agent runs on the factory computer that has access to the SQLite database. Its job is to read the database safely, prepare the requested data, convert it to transferable files, and upload those files to the Receiver API.

In the recommended deployment, the Factory computer connects to the Receiver through a WireGuard VPN.

Example:

```text
Factory local network address: unchanged
Factory WireGuard address:     10.10.0.2
Receiver WireGuard address:    10.10.0.3
WireGuard server address:      10.10.0.1
```

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

The Receiver calculates the checksum again after upload. If the checksum does not match, the file is rejected. The Factory Agent automatically retries the upload according to its retry settings, but if all retries fail, the transfer is not considered successful and the user must rerun the request or use the repair workflow.

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

- Windows 11
- Python 3.11 or newer
- `uv`
- WireGuard for Windows
- Access to the SQLite database file
- Outbound UDP access to the WireGuard server on port `51820`
- Port `9000` available on the Factory computer

Install `uv` if needed:

```powershell
pip install uv
```

Install project dependencies:

```powershell
cd factory_agent
uv sync
```

## Configure WireGuard on the Factory computer

### 1. Install WireGuard

Install WireGuard for Windows from the official WireGuard website.

### 2. Create a Factory tunnel

Open WireGuard and select:

```text
Add Tunnel
→ Add empty tunnel
```

WireGuard creates a private key and shows the corresponding public key.

Keep the private key only on the Factory computer. Copy the public key to the Ubuntu WireGuard server configuration.

Example Factory configuration:

```ini
[Interface]
PrivateKey = FACTORY_PRIVATE_KEY
Address = 10.10.0.2/24

[Peer]
PublicKey = SERVER_PUBLIC_KEY
Endpoint = SERVER_PUBLIC_IP:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

Replace:

- `FACTORY_PRIVATE_KEY` with the Factory private key,
- `SERVER_PUBLIC_KEY` with the Ubuntu server public key,
- `SERVER_PUBLIC_IP` with the public IP or DNS name of the WireGuard server.

`AllowedIPs = 10.10.0.0/24` creates a split tunnel. Only VPN traffic uses WireGuard. The Factory computer keeps using its existing local network for factory devices, PLCs, Modbus devices, and Internet access.

### 3. Add the Factory public key to the server

The Ubuntu server must contain:

```ini
[Peer]
PublicKey = FACTORY_PUBLIC_KEY
AllowedIPs = 10.10.0.2/32
```

Restart WireGuard on the Ubuntu server:

```bash
sudo systemctl restart wg-quick@wg0
sudo wg
```

### 4. Activate the Factory tunnel

In WireGuard for Windows, select the Factory tunnel and click:

```text
Activate
```

The tunnel should show transferred bytes. On the Ubuntu server, `sudo wg` should show a recent handshake.

### 5. Test Receiver connectivity

After the Receiver tunnel is also active:

```powershell
curl.exe http://10.10.0.3:8000
```

A successful HTTP response confirms that the Factory can reach the Receiver through WireGuard.

A failed ping does not necessarily indicate a VPN failure because Windows Firewall may block ICMP. An HTTP test is more useful.

## Configure Factory Agent

Update `config.yaml` so the Agent uploads to the Receiver WireGuard address:

```yaml
api_base_url: http://10.10.0.3:8000
```

Also set the real SQLite database path:

```yaml
sqlite_path: D:/path/to/fuse_monitoring.db
```

Recommended production setting:

```yaml
use_snapshot: true
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

The same key values must be configured on both systems.

## Run locally without WireGuard

Use this only when Agent and Receiver are on the same machine or directly reachable local network.

```powershell
$env:APP_ENV="local"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd factory_agent
uv run python -m agent.main --mode server --config config.yaml --host 127.0.0.1 --port 9000
```

## Run through WireGuard

First confirm:

- the Factory WireGuard tunnel is active,
- the Factory has VPN address `10.10.0.2`,
- the Receiver is reachable at `10.10.0.3`,
- `api_base_url` is set to `http://10.10.0.3:8000`.

Then run:

```powershell
$env:APP_ENV="production"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd factory_agent
uv sync
uv run python -m agent.main --mode server --config config.yaml --host 10.10.0.2 --port 9000
```


## Windows Firewall rule for Factory Agent

Run PowerShell as Administrator:

```powershell
New-NetFirewallRule `
  -DisplayName "Factory Agent API 9000 from WireGuard" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 9000 `
  -RemoteAddress 10.10.0.3
```

This permits port `9000` only from the Receiver VPN address.

Verify the port after starting the Agent:

```powershell
Test-NetConnection 10.10.0.2 -Port 9000
```

From the Receiver, test:

```powershell
curl.exe http://10.10.0.2:9000
```


