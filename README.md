# NVOS InfiniBand Fabric Port Disable Tool

Disables specified ports on NVOS InfiniBand switches via the NVUE REST API. Handles multiple switches concurrently and produces a report printed to the console and saved as a CSV file.

## Requirements

- Python 3.10+
- Network access to NVOS switch management interfaces (HTTPS, default port 443)
- NVUE REST API enabled on target switches

## Install

```bash
pip install -r requirements.txt
```

## Usage

### Disable ports on a single switch

```bash
python nvos_port_disable.py -u admin -p mypassword -t 10.0.0.1:sw1p1,sw1p2
```

### Disable ports on multiple switches

```bash
python nvos_port_disable.py -u admin -p mypassword \
  -t 10.0.0.1:sw1p1,sw1p2 \
  -t 10.0.0.2:sw3p1,sw3p2 \
  -t 10.0.0.3:sw2p1
```

### Use a JSON file for targets

```bash
python nvos_port_disable.py -u admin -p mypassword -f targets.json
```

Where `targets.json` contains:

```json
{
  "targets": {
    "10.0.0.1": ["sw1p1", "sw1p2"],
    "10.0.0.2": ["sw3p1", "sw3p2"],
    "10.0.0.3": ["sw2p1"]
  }
}
```

### Prompt for password (omit -p)

```bash
python nvos_port_disable.py -u admin -t 10.0.0.1:sw1p1
```

### Dry run (preview without making changes)

```bash
python nvos_port_disable.py -u admin -p mypassword -t 10.0.0.1:sw1p1 --dry-run
```

### Custom output file

```bash
python nvos_port_disable.py -u admin -p mypassword -t 10.0.0.1:sw1p1 -o my_report.csv
```

## Options

| Flag | Description |
|------|-------------|
| `-u, --username` | Username for switch authentication (required) |
| `-p, --password` | Password (prompts interactively if omitted) |
| `-t, --target` | `IP:port1,port2,...` — repeatable for multiple switches |
| `-f, --file` | JSON file with target definitions |
| `-o, --output` | Output CSV file path (auto-generated if omitted) |
| `--api-port` | NVUE REST API port (default: 443) |
| `--workers` | Max concurrent switch connections (default: 5) |
| `--dry-run` | Preview actions without making changes |

## Report

The tool prints a summary table to the console and saves a CSV with these columns:

| Column | Description |
|--------|-------------|
| `timestamp` | When the action was performed |
| `switch_ip` | Management IP of the switch |
| `port` | Interface name (e.g. `sw1p1`) |
| `previous_state` | Port state before the action (`up`, `down`, `not_found`) |
| `action` | What was attempted (`disable`, `skip`, `none`) |
| `result` | Outcome (`SUCCESS`, `FAILED`, `SKIPPED`) |
| `error` | Error details if the action failed |

## How It Works

1. Queries each port's current state via `GET /nvue_v1/interface/{port}/link/state`
2. Skips ports that are already down or don't exist
3. Creates a NVUE revision via `POST /nvue_v1/revision`
4. Batches all port disable operations into a single `PATCH /nvue_v1/interface?rev={changeset}`
5. Applies the revision via `PATCH /nvue_v1/revision/{changeset}`
6. Polls until the revision is applied
7. Verifies each port is now down
8. Generates the report
