# Industrial Data Transfer API

This project transfers raw data from a factory-side SQLite database to a receiver-side storage system. The main goal is to collect reliable raw data.

## Network architecture


Architecture:

```text
Factory private network
┌──────────────────────────────────────────┐                         
│ Factory computer                         │
│ - Existing local network address         │
│ - WireGuard address: 10.10.0.2           │
│ - Factory Agent API: port 9000           │
└──────────────────────────────────────────┘
                 ^
                 | Outbound WireGuard tunnel
                 | UDP 51820
                 v
┌──────────────────────────────────────────┐
│ Public server                            │
│ - WireGuard hub: 10.10.0.1               │
│ - Routes traffic between both peers      │
└──────────────────────────────────────────┘
                 ^
                 | Outbound WireGuard tunnel
                 | UDP 51820
                 v
Receiver private network
┌──────────────────────────────────────────┐
│ Receiver computer                        │
│ - Existing local network address         │
│ - WireGuard address: 10.10.0.3           │
│ - Receiver API and UI: port 8000         │
└──────────────────────────────────────────┘
```

The public server only forwards encrypted network packets.

## Main workflow

```text
User opens the Receiver web application
        |
        v
User selects a request type
   Examples:
   - Sync New Data
   - Full Database
   - Selected Tables
   - Custom Query
   - Inspect Schema
        |
        v
Receiver API sends the request through WireGuard
   to the Factory Agent at 10.10.0.2:9000
        |
        v
Factory Agent connects to the SQLite database
        |
        v
Factory Agent inspects the database schema
   - Tables
   - Columns
   - Primary keys
   - Schema versions
        |
        v
Factory Agent reads the required rows
   - Only new rows for incremental sync
   - Full table content for full refresh
   - Filtered rows for custom query
        |
        v
Factory Agent prepares transferable files
   - Converts data to Parquet
   - Adds metadata
   - Calculates SHA256 checksum
   - Splits large transfers into numbered part files
        |
        v
Factory Agent uploads files through WireGuard
   to the Receiver API at 10.10.0.3:8000
        |
        v
Receiver verifies each uploaded file
   - Validates metadata
   - Checks checksum
   - Rejects corrupted files
        |
        v
Receiver stores the result
   - Parquet files are saved in structured folders
   - Metadata is saved next to the files
   - Metadata is also registered in the Receiver database
        |
        v
Receiver UI shows the result
   - Total received rows
   - Number of part files
   - File names
   - Storage paths
   - Checksums
   - Repair or warning messages if needed
```

## Project parts

```text
industrial-data-transfer-api/
  factory_agent/    Reads the factory SQLite database and sends exported data
  receiver_api/     Provides the web UI, receives files, verifies them, and stores them
```

## WireGuard server setup on Ubuntu

The server can be any Ubuntu system with:

- a public IPv4 address,
- outbound Internet access,
- inbound UDP port `51820`,
- IP forwarding enabled.

The steps below assume Ubuntu 24.04 or a compatible Ubuntu release.

### 1. Open UDP port 51820

This setup assumes that the Ubuntu WireGuard server is hosted on Google Cloud.

In addition to the Ubuntu `ufw` rule, Google Cloud must allow inbound UDP traffic on port `51820` through a VPC firewall rule.

Create the Google Cloud firewall rule with the following settings:

```text
Name: allow-wireguard
Network: default
Direction: Ingress
Action: Allow
Targets: All instances in the network
Source IPv4 ranges: 0.0.0.0/0
Protocol: UDP
Port: 51820
```

You can create this rule in Google Cloud Console:

```text
Google Cloud Console
→ VPC network
→ Firewall
→ Create firewall rule
```

To verify the firewall rule:

```bash
gcloud compute firewall-rules describe allow-wireguard
```

Both firewall layers must allow WireGuard traffic:

```text
Google Cloud VPC firewall: UDP 51820 inbound allowed
Ubuntu UFW firewall:       UDP 51820 inbound allowed
```

Only UDP `51820` is needed for WireGuard.

### 2. Install WireGuard

Connect to the server through SSH and run:

```bash
sudo apt update
sudo apt install wireguard -y
```

Check the installation:

```bash
wg --version
```

### 3. Generate the server keys

```bash
sudo umask 077
sudo sh -c 'wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key'
```

Display only the public key when it is needed:

```bash
sudo cat /etc/wireguard/server_public.key
```

Do not share or commit the private key.

### 4. Enable IPv4 forwarding

Create a persistent sysctl configuration:

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wireguard-forwarding.conf
sudo sysctl --system
```

Verify:

```bash
sudo sysctl net.ipv4.ip_forward
```

Expected output:

```text
net.ipv4.ip_forward = 1
```

The virtual machine or server platform may also have an IP forwarding option. Enable it if the platform requires it.

### 5. Create the WireGuard configuration

Read the server private key locally on the server:

```bash
sudo cat /etc/wireguard/server_private.key
```

Create the configuration file:

```bash
sudo nano /etc/wireguard/wg0.conf
```

If `nano` is not installed:

```bash
sudo apt install nano -y
```

Example configuration:

```ini
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = SERVER_PRIVATE_KEY

[Peer]
# Factory Agent
PublicKey = FACTORY_PUBLIC_KEY
AllowedIPs = 10.10.0.2/32

[Peer]
# Receiver API
PublicKey = RECEIVER_PUBLIC_KEY
AllowedIPs = 10.10.0.3/32
```

Replace:

- `SERVER_PRIVATE_KEY` with the server private key,
- `FACTORY_PUBLIC_KEY` with the Factory computer public key,
- `RECEIVER_PUBLIC_KEY` with the Receiver computer public key.

Protect the configuration:

```bash
sudo chmod 600 /etc/wireguard/wg0.conf
```

### 6. Start WireGuard

```bash
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
```

Check the interface:

```bash
sudo wg
```

The output should show:

```text
interface: wg0
listening port: 51820
```

After both Windows peers connect, each peer should also show:

```text
latest handshake: ...
transfer: ...
```

### 7. Restart after configuration changes

After adding or changing peers:

```bash
sudo systemctl restart wg-quick@wg0
sudo wg
```

### 8. Security note

Each system must have its own WireGuard key pair.

Never reuse or publish:

- the server private key,
- the Factory private key,
- the Receiver private key.

If a private key is exposed, generate a new key pair and update all affected peer configurations.


## What the system handles

- Creating a secure encrypted WireGuard tunnel
- Requesting only new data.
- Requesting full database snapshots.
- Requesting selected tables.
- Running controlled custom queries.
- Inspecting schema without transferring row data.
- Detecting schema changes.
- Splitting large transfers into readable part files.
- Verifying file integrity with checksums.
- Detecting missing files in Receiver storage.
- Repairing missing files when possible.
