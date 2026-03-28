# NVOS InfiniBand Fabric Port Disable Tool

Disables specified ports on NVOS InfiniBand switches via the NVUE REST API. Optionally persists the applied configuration to startup (equivalent to `nv config save`). Handles multiple switches concurrently and produces a report printed to the console and saved as a CSV file.

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

#### JSON shape

The file must include a top-level `"targets"` object: each key is a switch management IP (string), each value is a JSON array of NVUE interface names (for example `sw37p1`, `sw38p2`).

```json
{
  "targets": {
    "10.0.0.1": ["sw1p1", "sw1p2"],
    "10.0.0.2": ["sw3p1", "sw3p2"],
    "10.0.0.3": ["sw2p1"]
  }
}
```

A fuller example with optional per-switch credentials is in [`example.json`](example.json) in this repository (placeholder IPs and passwords only).

#### Per-switch passwords and usernames

If every switch shares the same account, use `-u` and `-p` as usual. When passwords (or usernames) differ by switch, you can supply them in JSON instead of relying on a single `-p` value.

**Option A — embedded in the targets file**  
Add an optional top-level `"credentials"` object. Keys are the same switch IPs as in `"targets"`. Each value is an object with optional `"username"` and/or `"password"`. Any field you omit for a given IP falls back to `-u` / `-p` from the command line.

```json
{
  "targets": {
    "10.0.0.1": ["sw1p1"],
    "10.0.0.2": ["sw2p1"]
  },
  "credentials": {
    "10.0.0.1": { "password": "secret-for-switch-one" },
    "10.0.0.2": { "username": "otheradmin", "password": "secret-for-switch-two" }
  }
}
```

**Option B — separate credentials file**  
Pass `-c` / `--credentials-file` with a JSON file whose top level is only the IP → credential map (no `"targets"` wrapper):

```json
{
  "10.0.0.1": { "password": "secret-for-switch-one" },
  "10.0.0.2": { "password": "secret-for-switch-two" }
}
```

```bash
python nvos_port_disable.py -u admin -p default_shared_secret -f targets.json -c switch_credentials.json
```

Switches not listed under `"credentials"` (embedded or `-c`) still use the CLI username and password. If you use both embedded credentials and `-c` for the same IP, **the `-c` file wins** for that IP.

**Security:** real credential files should stay out of version control (for example list them in `.gitignore`). The bundled `example.json` uses obvious placeholders and documentation-only IPs ([TEST-NET-3](https://datatracker.ietf.org/doc/html/rfc5737)).

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

### Save to startup after disables (single run)

After at least one successful port disable on a switch, the tool can write the **applied** NVUE configuration to startup (`PATCH /nvue_v1/revision/applied` with `state: save`), same idea as `nv config save`:

```bash
python nvos_port_disable.py -u admin -p mypassword -t 10.0.0.1:sw1p1,sw1p2 --save-config
```

Switches with no successful disable in that run are not saved.

### Two-step: disable first, save later

Use this when you want to disable ports, verify the fabric, then persist configuration in a second run.

1. Disable only (omit `--save-config`):

```bash
python nvos_port_disable.py -u admin -p mypassword -f targets.json
```

2. After your checks, save applied config on each listed switch only (no port changes):

```bash
python nvos_port_disable.py -u admin -p mypassword --save-only -f targets.json
```

`--save-config-only` is the same as `--save-only`. With save-only mode you can pass bare switch IPs: `-t 10.0.0.1 -t 10.0.0.2`. Using `-f` saves once per key under `targets` (port lists in the file are ignored for this step).

`--save-config` cannot be combined with `--save-only` / `--save-config-only`.

## Options

| Flag | Description |
|------|-------------|
| `-u, --username` | Username for switch authentication (required) |
| `-p, --password` | Password (prompts interactively if omitted) |
| `-t, --target` | `IP:port1,port2,...` — repeatable for multiple switches. With `--save-only` / `--save-config-only`, `IP` alone (no port list) is allowed |
| `-f, --file` | JSON file with `targets` (required) and optional embedded `credentials` per IP |
| `-c, --credentials-file` | JSON file mapping each switch IP to optional `username` / `password`; overrides embedded credentials for the same IP |
| `-o, --output` | Output CSV file path (auto-generated if omitted) |
| `--api-port` | NVUE REST API port (default: 443) |
| `--workers` | Max concurrent switch connections (default: 5) |
| `--dry-run` | Preview actions without making changes |
| `--save-config` | After a successful disable on a switch, persist applied config to startup on that switch |
| `--save-only`, `--save-config-only` | Save applied config to startup only; no disables. Mutually exclusive with `--save-config` |

## Report

The tool prints a summary table to the console and saves a CSV with these columns:

| Column | Description |
|--------|-------------|
| `timestamp` | When the action was performed |
| `switch_ip` | Management IP of the switch |
| `port` | Interface name (e.g. `sw1p1`), or `*` for a save-to-startup row |
| `previous_state` | Port state before the action (`up`, `down`, `not_found`), or `n/a` for save rows |
| `action` | What was attempted (`disable`, `skip`, `none`, `save`) |
| `result` | Outcome (`SUCCESS`, `FAILED`, `SKIPPED`) |
| `error` | Error details if the action failed |

## How It Works

### Port disable (default)

1. Queries each port's current state via `GET /nvue_v1/interface/{port}/link/state`
2. Skips ports that are already down or don't exist
3. Creates a NVUE revision via `POST /nvue_v1/revision`
4. Batches all port disable operations into a single `PATCH /nvue_v1/interface?rev={changeset}`
5. Applies the revision via `PATCH /nvue_v1/revision/{changeset}`
6. Polls until the revision is applied
7. Verifies each port is now down
8. If `--save-config` and that switch had at least one successful disable, issues `PATCH /nvue_v1/revision/applied` with `state: save` (and the same auto-prompt payload used for apply)
9. Generates the report

### Save only (`--save-only` / `--save-config-only`)

For each target switch, issues `PATCH /nvue_v1/revision/applied` with `state: save` only (no interface changes), then generates the report.
