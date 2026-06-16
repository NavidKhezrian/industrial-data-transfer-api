# Industrial Data Transfer Factory Installer

## Purpose

`FactoryDeploymentInstaller.exe` is an all-in-one graphical Windows installer for the **Industrial Data Transfer Factory Agent**.

The goal is to install and configure the local Factory Agent on a Windows factory computer so that a remote Receiver system can request data from a local SQLite database through a restricted WireGuard VPN connection.

## Source files

```text
Build-FactoryDeploymentInstaller.ps1
src/
  FactoryBootstrap.ps1
  FactoryDeploymentInstallerGui.ps1
```

The installer is generated from:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force

.\Build-FactoryDeploymentInstaller.ps1 `
  -WireGuardServerEndpoint "PUBLIC_SERVER_IP_OR_DNS:51820" `
  -WireGuardServerPublicKey "SERVER_PUBLIC_KEY"
```

Output:

```text
dist\FactoryDeploymentInstaller.exe
```

Transfer only the EXE to the factory computer. If a `.exe.config` file is produced by the build tool, keep it next to the EXE.



## Administrator rights

The EXE must be run with Administrator privileges:

```text
Right click → Run as administrator
```

Administrator rights are required because the installer may:

- install WireGuard for Windows,
- install or configure runtime tools,
- write to machine-level installation folders,
- add directories to the machine PATH,
- create a WireGuard tunnel service,
- create Windows Firewall rules,
- create a Windows Scheduled Task running as SYSTEM,
- start Windows services and scheduled tasks.

---

## User inputs requested by the graphical installer

The installer GUI asks for:

1. **Installation folder**

   Default:

   ```text
   C:\IndustrialDataTransfer
   ```

   The operator can change this path using the Browse button.

2. **SQLite database file**

   The operator selects the local factory SQLite database using a Browse button.

   Supported examples:

   ```text
   *.db
   *.sqlite
   *.sqlite3
   ```

3. **WireGuard endpoint**

   This must be the public IP address or DNS name of the WireGuard server plus port.

   Example:

   ```text
   35.10.20.30:51820
   ```

4. **WireGuard server public key**

   This is the public key of the Ubuntu WireGuard server.

The endpoint and server public key can also be embedded into the EXE when the EXE is built. In that case, the fields are already pre-filled in the GUI.

---

## What the EXE contains

The generated EXE contains the required installer logic internally. It extracts and runs the internal components during installation.

It includes:

- graphical installer UI,
- Factory Agent bootstrap installer,
- service-only manager generator,
- logic to create `FactoryServiceManager.exe` after installation.

The factory operator does not need to copy separate PowerShell files to the factory computer.

---

## What the installer does on the factory computer

During installation, the EXE performs the following operations.

### 1. Creates the installation directory

The selected installation folder is created if it does not already exist.

Example:

```text
D:\TTZ\Bruchmann\test2
```

The installer then creates subfolders such as:

```text
app\
logs\
python\
python-bin\
runtime\
secrets\
tools\
wireguard\
```

---

### 2. Installs or verifies WireGuard for Windows

The installer checks whether WireGuard is already installed under:

```text
C:\Program Files\WireGuard\
```

If WireGuard is missing, the installer attempts to install it.

The installer may use:

- `winget`, if available, or
- the official WireGuard MSI download source.

When using the MSI method, the installer validates the digital signature of the downloaded WireGuard MSI before installation.

---

### 3. Installs or verifies `uv` and Python

The project requires Python 3.11 or newer.

The installer checks whether a suitable 64-bit Python installation is already available.

If no suitable Python is found, it installs a managed Python runtime using `uv` under the installation/runtime area. This avoids modifying or relying on unrelated Python installations already used by factory software.

The installer may add required executable directories to the machine PATH only when those executables are not already discoverable.

---

### 4. Downloads or updates the project source code

The installer downloads the Factory Agent project from GitHub:

```text
https://github.com/NavidKhezrian/industrial-data-transfer-api.git
```

It uses the `main` branch.

If Git is available, it uses Git clone/fetch. If Git is not available, it can use a ZIP download fallback.

The project is installed under:

```text
<Installation folder>\app\industrial-data-transfer-api
```

---

### 5. Installs Python dependencies

The installer runs dependency installation for the Factory Agent using `uv sync`.

This creates a Python virtual environment for the project and installs required packages such as FastAPI, Uvicorn, pandas, PyArrow, requests, and related dependencies.

A warning like the following is not fatal:

```text
Failed to hardlink files; falling back to full copy
```

It only means `uv` used normal file copy instead of hardlinks.

---

### 6. Validates the selected SQLite database

The installer tests the selected SQLite database in read-only mode.

The installer does not modify the selected SQLite database during this validation.

The validation checks that:

- the file exists,
- it can be opened as SQLite,
- it can be read,
- basic table metadata can be accessed.

---

### 7. Generates local secrets

The installer creates or reuses a secrets file:

```text
<Installation folder>\secrets\agent-secrets.json
```

This file contains API keys used between the Receiver and the Factory Agent:

```text
FACTORY_AGENT_API_KEY
RECEIVER_API_KEY
```

If the installer is run again, existing keys are reused when possible. This prevents breaking an already configured Receiver.

---

### 8. Generates the Factory WireGuard key pair

The installer creates a WireGuard private/public key pair for the factory computer.

The Factory private key remains on the factory computer.

The Factory public key is written into the share file described below so that the WireGuard server administrator can add this factory computer as a peer.

---

### 9. Writes WireGuard tunnel configuration

The installer writes a WireGuard configuration file under:

```text
<Installation folder>\wireguard\factory-agent.conf
```

The expected Factory VPN address is:

```text
10.10.0.2/24
```

The expected Allowed IP range is:

```text
10.10.0.0/24
```

The configuration uses:

```text
PersistentKeepalive = 25
```

This helps keep the connection alive when the factory computer is behind NAT.

---

### 10. Installs the WireGuard tunnel as a Windows service

The installer installs the tunnel as a Windows service named:

```text
WireGuardTunnel$factory-agent
```

This service is started by the installer.

It can be checked with:

```powershell
Get-Service 'WireGuardTunnel$factory-agent'
```

---

### 11. Creates restricted Windows Firewall rules

The installer creates a restricted inbound rule for the Factory Agent API:

```text
Local address: 10.10.0.2
Local TCP port: 9000
Remote address allowed: 10.10.0.3
```

This means the Factory Agent API is intended to be reachable only from the Receiver VPN IP.

The firewall rule name is:

```text
Factory Agent API 9000 from WireGuard
```

If the Windows Firewall outbound policy blocks outbound connections, the installer also creates an outbound UDP rule for WireGuard traffic to the public server endpoint and port.

The outbound firewall rule name is:

```text
WireGuard to Public Server UDP 51820
```

---

### 12. Configures the Factory Agent

The installer updates the Factory Agent configuration to use:

```text
environment: production
SQLite database path: the selected database file
Receiver URL: http://10.10.0.3:8000
```

The Factory Agent listens on:

```text
http://10.10.0.2:9000
```

---

### 13. Starts the Factory Agent and performs health checks

The installer starts the Factory Agent and tests the local health endpoint:

```text
http://10.10.0.2:9000/health
```

It also attempts to validate database access through the Factory Agent.

If the WireGuard server has not yet been configured with the Factory public key, full VPN communication may not work yet. This is expected until the server-side peer is added.

---

### 14. Creates a service-only executable

After successful installation, the installer creates:

```text
<Installation folder>\FactoryServiceManager.exe
```

This file is only for starting the required services later.

It does not reinstall the application.

It is useful after a restart or if the services were stopped manually.

It starts/checks:

```text
WireGuardTunnel$factory-agent
IndustrialDataTransfer-FactoryAgent
```

---


## Important output files

### `PLEASE_SEND_THIS_FILE_TO_US.txt`

It includes:

- Factory WireGuard public key,
- Factory VPN address,
- Factory Agent URL,
- Receiver URL,
- generated API keys,
- server-side configuration instructions.

Security note: because this file contains API keys, it should be handled carefully and removed after the server and Receiver have been configured, if no longer needed.



---

## Required server-side action after first installation

The first installation generates the Factory WireGuard key pair locally. Therefore, the public WireGuard server does not know the Factory public key in advance.

After the first installation, the administrator must add the Factory public key from:

```text
PLEASE_SEND_THIS_FILE_TO_US.txt
```

to the Ubuntu WireGuard server configuration, typically:

```text
/etc/wireguard/wg0.conf
```

Example server-side peer entry:

```ini
[Peer]
PublicKey = FACTORY_PUBLIC_KEY
AllowedIPs = 10.10.0.2/32
```

Then restart WireGuard on the server:

```bash
sudo systemctl restart wg-quick@wg0
sudo wg
```

A successful connection should show a recent handshake for the Factory peer.


## Uninstall / Rollback Notes

If rollback is required, IT can run PowerShell as Administrator and use the commands below.

### Stop running components

```powershell
Stop-ScheduledTask -TaskName 'IndustrialDataTransfer-FactoryAgent' -ErrorAction SilentlyContinue
```

Stops the Factory Agent Scheduled Task, if it exists.

```powershell
Stop-Service 'WireGuardTunnel$factory-agent' -ErrorAction SilentlyContinue
```

Stops the WireGuard tunnel service, if it is running.

### Remove installed Windows components

```powershell
Unregister-ScheduledTask -TaskName 'IndustrialDataTransfer-FactoryAgent' -Confirm:$false -ErrorAction SilentlyContinue
```
Removes the WireGuard tunnel service named `factory-agent`.

```powershell
Remove-NetFirewallRule -DisplayName 'Factory Agent API 9000 from WireGuard' -ErrorAction SilentlyContinue
```

Removes the inbound firewall rule for the Factory Agent API on TCP port `9000`.

```powershell
Remove-NetFirewallRule -DisplayName 'WireGuard to Public Server UDP 51820' -ErrorAction SilentlyContinue
```

Removes the outbound firewall rule for WireGuard traffic to the public server on UDP port `51820`.

### Installation folder

After these steps, the installation folder selected during setup can be deleted manually if it is no longer needed.


