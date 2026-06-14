# Receiver API

The Receiver API runs on the receiving side. It provides the browser web application, sends requests to the Factory Agent, receives uploaded Parquet files, verifies them, stores them, and tracks metadata.

In the recommended deployment, the Receiver connects to the Factory Agent through WireGuard.

Example:

```text
Factory Agent WireGuard address: 10.10.0.2
Receiver WireGuard address:      10.10.0.3
WireGuard server address:        10.10.0.1
```

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

### Full Refresh Selected Tables

Use this when selected tables must be copied completely.

### Custom Query

Use this to export a controlled subset of one table.

The UI allows selecting columns and filters. Free SQL is not sent directly.

### Inspect Schema

Use this to read only table and column information.

No row data is transferred.

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

## Handling missing files

### Repair can be tried

These files have enough metadata and the source table appears to exist. Receiver can ask the Factory Agent to recreate them.

### Run Custom Query again

This applies to older Custom Query files that were created before the full query definition was stored in metadata.

### Cannot repair automatically

These files cannot be recreated safely from the current source database. A common reason is that the source table no longer exists.

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

- Windows 11
- Python 3.11 or newer
- `uv`
- WireGuard for Windows
- Outbound UDP access to the WireGuard server on port `51820`
- Port `8000` available on the Receiver computer
- Enough disk space for Parquet storage and metadata

Install `uv` if needed:

```powershell
pip install uv
```

Install project dependencies:

```powershell
cd receiver_api
uv sync
```

## Configure WireGuard on the Receiver computer

### 1. Install WireGuard

Install WireGuard for Windows from the official WireGuard website.

### 2. Create a Receiver tunnel

Open WireGuard and select:

```text
Add Tunnel
→ Add empty tunnel
```

WireGuard creates a private key and shows the corresponding public key.

Keep the private key only on the Receiver computer. Copy the public key to the Ubuntu WireGuard server configuration.

Example Receiver configuration:

```ini
[Interface]
PrivateKey = RECEIVER_PRIVATE_KEY
Address = 10.10.0.3/24

[Peer]
PublicKey = SERVER_PUBLIC_KEY
Endpoint = SERVER_PUBLIC_IP:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

Replace:

- `RECEIVER_PRIVATE_KEY` with the Receiver private key,
- `SERVER_PUBLIC_KEY` with the Ubuntu server public key,
- `SERVER_PUBLIC_IP` with the public IP or DNS name of the WireGuard server.

`AllowedIPs = 10.10.0.0/24` creates a split tunnel. The Receiver keeps using its existing local network and Internet connection normally.

### 3. Add the Receiver public key to the server

The Ubuntu server must contain:

```ini
[Peer]
PublicKey = RECEIVER_PUBLIC_KEY
AllowedIPs = 10.10.0.3/32
```

Restart WireGuard on the Ubuntu server:

```bash
sudo systemctl restart wg-quick@wg0
sudo wg
```

### 4. Activate the Receiver tunnel

In WireGuard for Windows, select the Receiver tunnel and click:

```text
Activate
```

The Ubuntu server should show a recent handshake for the Receiver peer.

### 5. Test Factory connectivity

After the Factory Agent tunnel is also active:

```powershell
curl.exe http://10.10.0.2:9000
```

A successful HTTP response confirms that the Receiver can reach the Factory through WireGuard.

A failed ping may only mean that Windows Firewall blocks ICMP. Test the actual TCP port instead:

```powershell
Test-NetConnection 10.10.0.2 -Port 9000
```

## Configure Receiver API

Update `config.yaml` so Receiver calls the Factory Agent through its WireGuard address.

Use the actual configuration key used by the project for the Factory Agent base URL. Set its value to:

```yaml
http://10.10.0.2:9000
```

For example, if the key is named `factory_agent_url`:

```yaml
factory_agent_url: http://10.10.0.2:9000
```

Keep Receiver storage paths on the Receiver computer. The WireGuard server does not store application files.

## API keys

Two API keys are used:

| Key | Used by |
|---|---|
| `RECEIVER_API_KEY` | Used to access Receiver UI/API and by Factory Agent when uploading files. |
| `FACTORY_AGENT_API_KEY` | Used by Receiver when calling the Factory Agent. |

The same values must be configured on both systems.

## Run locally without WireGuard

Use this only when the browser and Receiver API are on the same computer and no remote Factory access is required.

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

## Run through WireGuard

First confirm:

- the Receiver WireGuard tunnel is active,
- the Receiver has VPN address `10.10.0.3`,
- the Factory Agent is reachable at `10.10.0.2:9000`,
- the Factory Agent URL in `config.yaml` uses `10.10.0.2`.

Then run:

```powershell
$env:APP_ENV="production"
$env:RECEIVER_API_KEY="paste_receiver_key_here"
$env:FACTORY_AGENT_API_KEY="paste_factory_agent_key_here"

cd receiver_api
uv sync
uv run uvicorn api.main:app --host 10.10.0.3 --port 8000
```

Open the UI on the Receiver computer:

```text
http://10.10.0.3:8000
```

The Factory Agent uploads files to:

```text
http://10.10.0.3:8000
```

Binding specifically to `10.10.0.3` prevents Receiver from listening on every local network interface.

## Windows Firewall rule for Receiver API

Run PowerShell as Administrator:

```powershell
New-NetFirewallRule `
  -DisplayName "Receiver API 8000 from WireGuard" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8000 `
  -RemoteAddress 10.10.0.2
```

This permits port `8000` only from the Factory VPN address.

If the web UI must also be opened from another trusted local computer, create an additional restricted firewall rule for that local IP or subnet. Do not expose port `8000` to the public Internet.

Verify locally:

```powershell
Test-NetConnection 10.10.0.3 -Port 8000
```

From the Factory computer:

```powershell
curl.exe http://10.10.0.3:8000
```