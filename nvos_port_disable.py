#!/usr/bin/env python3
"""
NVOS InfiniBand Fabric Port Disable Tool

Disables specified ports on NVOS InfiniBand switches via the NVUE REST API.
Produces a final report printed to the console and saved as a CSV file.
"""

import argparse
import csv
import getpass
import json
import sys
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

POLL_INTERVAL = 1
POLL_RETRIES = 30
API_TIMEOUT = 30


@dataclass
class PortResult:
    switch_ip: str
    port: str
    previous_state: str
    action: str
    result: str
    error: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class NVUEClient:
    """REST API client for a single NVOS switch."""

    def __init__(self, ip: str, username: str, password: str, port: int = 443):
        self.ip = ip
        self.base_url = f"https://{ip}:{port}/nvue_v1"
        self.auth = HTTPBasicAuth(username, password)
        self.headers = {"Content-Type": "application/json"}
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.verify = False
        self.session.headers.update(self.headers)

    def _get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        return self.session.get(
            f"{self.base_url}{path}", params=params, timeout=API_TIMEOUT
        )

    def _post(self, path: str, data: Optional[dict] = None) -> requests.Response:
        return self.session.post(
            f"{self.base_url}{path}",
            data=json.dumps(data) if data else None,
            timeout=API_TIMEOUT,
        )

    def _patch(
        self, path: str, data: dict, params: Optional[dict] = None
    ) -> requests.Response:
        return self.session.patch(
            f"{self.base_url}{path}",
            data=json.dumps(data),
            params=params,
            timeout=API_TIMEOUT,
        )

    def get_hostname(self) -> str:
        try:
            r = self._get("/system")
            r.raise_for_status()
            return r.json().get("hostname", self.ip)
        except Exception:
            return self.ip

    def get_port_state(self, port_name: str) -> str:
        """Return the operational link state of an interface (e.g. 'up', 'down')."""
        try:
            r = self._get(f"/interface/{port_name}/link/state")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data.get("operational", data.get("applied", "unknown"))
            return str(data)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return "not_found"
            return "error"
        except Exception:
            return "error"

    def create_revision(self) -> str:
        r = self._post("/revision")
        r.raise_for_status()
        response = r.json()
        changeset = list(response.keys())[0]
        return changeset

    def apply_revision(self, changeset: str) -> None:
        payload = {"state": "apply", "auto-prompt": {"ays": "ays_yes"}}
        quoted = requests.utils.quote(changeset, safe="")
        r = self._patch(f"/revision/{quoted}", payload)
        r.raise_for_status()

    def wait_for_apply(self, changeset: str) -> bool:
        quoted = requests.utils.quote(changeset, safe="")
        for _ in range(POLL_RETRIES):
            r = self._get(f"/revision/{quoted}")
            r.raise_for_status()
            state = r.json().get("state", "")
            if state == "applied":
                return True
            if state in ("apply_failure", "ays_fail"):
                return False
            time.sleep(POLL_INTERVAL)
        return False

    def save_applied_config(self) -> None:
        """Persist applied configuration to startup (REST equivalent of nv config save)."""
        payload = {"state": "save", "auto-prompt": {"ays": "ays_yes"}}
        r = self._patch("/revision/applied", payload)
        r.raise_for_status()

    def disable_ports(self, ports: list[str]) -> list[PortResult]:
        """Disable a list of ports on this switch. Returns per-port results."""
        results: list[PortResult] = []
        hostname = self.get_hostname()
        label = f"{hostname} ({self.ip})" if hostname != self.ip else self.ip

        pre_states: dict[str, str] = {}
        for port_name in ports:
            pre_states[port_name] = self.get_port_state(port_name)

        valid_ports = []
        for port_name in ports:
            prev = pre_states[port_name]
            if prev == "not_found":
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state="not_found",
                        action="skip",
                        result="FAILED",
                        error=f"Interface {port_name} does not exist on {label}",
                    )
                )
            elif prev == "down":
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state="down",
                        action="none",
                        result="SKIPPED",
                        error="Port already down",
                    )
                )
            else:
                valid_ports.append(port_name)

        if not valid_ports:
            return results

        try:
            changeset = self.create_revision()
        except Exception as e:
            for port_name in valid_ports:
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="FAILED",
                        error=f"Failed to create revision on {label}: {e}",
                    )
                )
            return results

        try:
            patch_payload = {
                p: {"link": {"state": "down"}} for p in valid_ports
            }
            r = self._patch(
                "/interface", patch_payload, params={"rev": changeset}
            )
            r.raise_for_status()
        except Exception as e:
            for port_name in valid_ports:
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="FAILED",
                        error=f"Failed to patch interface config on {label}: {e}",
                    )
                )
            return results

        try:
            self.apply_revision(changeset)
            applied = self.wait_for_apply(changeset)
        except Exception as e:
            for port_name in valid_ports:
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="FAILED",
                        error=f"Failed to apply revision on {label}: {e}",
                    )
                )
            return results

        if not applied:
            for port_name in valid_ports:
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="FAILED",
                        error=f"Revision apply timed out or failed on {label}",
                    )
                )
            return results

        for port_name in valid_ports:
            post_state = self.get_port_state(port_name)
            if post_state == "down":
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="SUCCESS",
                        error="",
                    )
                )
            else:
                results.append(
                    PortResult(
                        switch_ip=self.ip,
                        port=port_name,
                        previous_state=pre_states[port_name],
                        action="disable",
                        result="FAILED",
                        error=f"Post-apply state is '{post_state}', expected 'down'",
                    )
                )

        return results


def parse_targets(
    target_args: list[str], *, allow_ip_only: bool = False
) -> dict[str, list[str]]:
    """
    Parse target arguments in the form 'IP:port1,port2,...'
    If allow_ip_only is True (for --save-only), 'IP' alone selects that switch with no ports.
    Returns dict mapping IP -> list of port names.
    """
    targets: dict[str, list[str]] = {}
    for entry in target_args:
        entry = entry.strip()
        if ":" not in entry:
            if not allow_ip_only:
                print(
                    f"ERROR: Invalid target format '{entry}'. "
                    "Expected IP:port1,port2,... (or use --save-only with -t IP)"
                )
                sys.exit(1)
            if not entry:
                print("ERROR: Empty --target entry")
                sys.exit(1)
            targets.setdefault(entry, [])
            continue
        ip, ports_str = entry.split(":", 1)
        ip = ip.strip()
        if not ip:
            print(f"ERROR: Invalid target format '{entry}'")
            sys.exit(1)
        ports = [p.strip() for p in ports_str.split(",") if p.strip()]
        if not ports and not allow_ip_only:
            print(f"ERROR: No ports specified for {ip}")
            sys.exit(1)
        targets.setdefault(ip, []).extend(ports)
    return targets


def _normalize_per_ip_credentials(raw: Any, context: str) -> dict[str, dict[str, str]]:
    """Validate and return IP -> {username?, password?} from a JSON object."""
    if not isinstance(raw, dict):
        print(f"ERROR: {context} must be a JSON object mapping IPs to credential objects")
        sys.exit(1)
    out: dict[str, dict[str, str]] = {}
    for ip, entry in raw.items():
        if not isinstance(ip, str) or not ip.strip():
            print(f"ERROR: {context} has invalid key (expected IP string): {ip!r}")
            sys.exit(1)
        if not isinstance(entry, dict):
            print(
                f"ERROR: {context} entry for {ip!r} must be an object "
                '(e.g. {"password": "..."} or {"username": "...", "password": "..."})'
            )
            sys.exit(1)
        row: dict[str, str] = {}
        if "username" in entry and entry["username"] is not None:
            row["username"] = str(entry["username"])
        if "password" in entry and entry["password"] is not None:
            row["password"] = str(entry["password"])
        unknown = set(entry.keys()) - {"username", "password"}
        if unknown:
            print(
                f"ERROR: {context} entry for {ip!r} has unknown keys: "
                f"{', '.join(sorted(unknown))}"
            )
            sys.exit(1)
        out[ip.strip()] = row
    return out


def load_targets_from_file(filepath: str) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """
    Load targets from a JSON file with the format:
    {
        "targets": {
            "10.0.0.1": ["sw1p1", "sw1p2"],
            "10.0.0.2": ["sw3p1"]
        },
        "credentials": {
            "10.0.0.1": {"password": "switch-specific-secret"},
            "10.0.0.2": {"username": "admin", "password": "other-secret"}
        }
    }

    Optional top-level "credentials" maps switch IP to objects with optional
    "username" and/or "password". Omitted fields fall back to -u / -p on the CLI.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    if "targets" not in data:
        print("ERROR: JSON file must have a 'targets' key mapping IPs to port lists")
        sys.exit(1)

    embedded: dict[str, dict[str, str]] = {}
    if "credentials" in data and data["credentials"] is not None:
        embedded = _normalize_per_ip_credentials(
            data["credentials"], f"\"credentials\" in {filepath}"
        )

    return data["targets"], embedded


def load_credentials_file(filepath: str) -> dict[str, dict[str, str]]:
    """
    Load per-switch credentials from a JSON file mapping IP to an object, e.g.:
    {
        "10.0.0.1": {"password": "secret1"},
        "10.0.0.2": {"username": "admin2", "password": "secret2"}
    }
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: Credentials file not found: {filepath}")
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    return _normalize_per_ip_credentials(data, filepath)


def merge_per_ip_credentials(
    *maps: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Later maps override earlier ones for the same IP."""
    merged: dict[str, dict[str, str]] = {}
    for m in maps:
        for ip, row in m.items():
            base = dict(merged.get(ip, {}))
            base.update(row)
            merged[ip] = base
    return merged


def resolve_switch_auth(
    ip: str,
    default_username: str,
    default_password: str,
    per_ip: dict[str, dict[str, str]],
) -> tuple[str, str]:
    row = per_ip.get(ip, {})
    username = row.get("username") or default_username
    password = row.get("password")
    if password is None or password == "":
        password = default_password
    return username, password


def process_switch(
    ip: str,
    ports: list[str],
    username: str,
    password: str,
    api_port: int,
    save_config: bool = False,
) -> list[PortResult]:
    """Process all port disables for a single switch."""
    try:
        client = NVUEClient(ip, username, password, port=api_port)
        results = client.disable_ports(ports)
        if save_config and any(
            r.action == "disable" and r.result == "SUCCESS" for r in results
        ):
            try:
                client.save_applied_config()
                results.append(
                    PortResult(
                        switch_ip=ip,
                        port="*",
                        previous_state="n/a",
                        action="save",
                        result="SUCCESS",
                        error="",
                    )
                )
            except Exception as e:
                results.append(
                    PortResult(
                        switch_ip=ip,
                        port="*",
                        previous_state="n/a",
                        action="save",
                        result="FAILED",
                        error=str(e),
                    )
                )
        return results
    except requests.exceptions.ConnectionError:
        return [
            PortResult(
                switch_ip=ip,
                port=p,
                previous_state="unknown",
                action="disable",
                result="FAILED",
                error=f"Connection refused or unreachable: {ip}",
            )
            for p in ports
        ]
    except Exception as e:
        return [
            PortResult(
                switch_ip=ip,
                port=p,
                previous_state="unknown",
                action="disable",
                result="FAILED",
                error=str(e),
            )
            for p in ports
        ]


def process_switch_save_only(
    ip: str, username: str, password: str, api_port: int
) -> list[PortResult]:
    """Persist applied NVUE configuration to startup for one switch (no port changes)."""
    try:
        client = NVUEClient(ip, username, password, port=api_port)
        client.save_applied_config()
        return [
            PortResult(
                switch_ip=ip,
                port="*",
                previous_state="n/a",
                action="save",
                result="SUCCESS",
                error="",
            )
        ]
    except requests.exceptions.ConnectionError:
        return [
            PortResult(
                switch_ip=ip,
                port="*",
                previous_state="n/a",
                action="save",
                result="FAILED",
                error=f"Connection refused or unreachable: {ip}",
            )
        ]
    except Exception as e:
        return [
            PortResult(
                switch_ip=ip,
                port="*",
                previous_state="n/a",
                action="save",
                result="FAILED",
                error=str(e),
            )
        ]


def print_report(results: list[PortResult]) -> None:
    """Print a formatted report to the console."""
    col_widths = {
        "switch_ip": max(10, max((len(r.switch_ip) for r in results), default=10)),
        "port": max(6, max((len(r.port) for r in results), default=6)),
        "previous_state": max(14, max((len(r.previous_state) for r in results), default=14)),
        "action": max(8, max((len(r.action) for r in results), default=8)),
        "result": max(8, max((len(r.result) for r in results), default=8)),
        "error": max(5, max((len(r.error) for r in results), default=5)),
    }

    header = (
        f"{'Switch IP':<{col_widths['switch_ip']}}  "
        f"{'Port':<{col_widths['port']}}  "
        f"{'Previous State':<{col_widths['previous_state']}}  "
        f"{'Action':<{col_widths['action']}}  "
        f"{'Result':<{col_widths['result']}}  "
        f"{'Error'}"
    )
    separator = "-" * len(header)

    success_count = sum(1 for r in results if r.result == "SUCCESS")
    failed_count = sum(1 for r in results if r.result == "FAILED")
    skipped_count = sum(1 for r in results if r.result == "SKIPPED")
    unique_switches = len(set(r.switch_ip for r in results))

    print("\n" + "=" * len(header))
    print("NVOS PORT DISABLE REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Switches: {unique_switches}  |  "
          f"Total Ports: {len(results)}  |  "
          f"Success: {success_count}  |  "
          f"Failed: {failed_count}  |  "
          f"Skipped: {skipped_count}")
    print("=" * len(header))
    print(header)
    print(separator)

    for r in sorted(results, key=lambda x: (x.switch_ip, x.port)):
        line = (
            f"{r.switch_ip:<{col_widths['switch_ip']}}  "
            f"{r.port:<{col_widths['port']}}  "
            f"{r.previous_state:<{col_widths['previous_state']}}  "
            f"{r.action:<{col_widths['action']}}  "
            f"{r.result:<{col_widths['result']}}  "
            f"{r.error}"
        )
        print(line)

    print(separator)
    print()


def save_csv(results: list[PortResult], filepath: str) -> None:
    """Save results to a CSV file."""
    fieldnames = [
        "timestamp",
        "switch_ip",
        "port",
        "previous_state",
        "action",
        "result",
        "error",
    ]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(results, key=lambda x: (x.switch_ip, x.port)):
            writer.writerow(
                {
                    "timestamp": r.timestamp,
                    "switch_ip": r.switch_ip,
                    "port": r.port,
                    "previous_state": r.previous_state,
                    "action": r.action,
                    "result": r.result,
                    "error": r.error,
                }
            )


def main():
    parser = argparse.ArgumentParser(
        description="Disable ports on NVOS InfiniBand switches via NVUE REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Disable ports on a single switch
  %(prog)s -u admin -p password -t 10.0.0.1:sw1p1,sw1p2

  # Disable ports on multiple switches
  %(prog)s -u admin -p password -t 10.0.0.1:sw1p1,sw1p2 -t 10.0.0.2:sw3p1

  # Use a JSON file for targets
  %(prog)s -u admin -p password -f targets.json

  # Per-switch passwords (JSON map IP -> {password}; -p used for any IP omitted)
  %(prog)s -u admin -p default_secret -f targets.json -c switch_passwords.json

  # Prompt for password
  %(prog)s -u admin -t 10.0.0.1:sw1p1

  # Custom output file and API port
  %(prog)s -u admin -p password -t 10.0.0.1:sw1p1 -o report.csv --api-port 8443

  # Save applied config to startup after successful disables (nv config save)
  %(prog)s -u admin -p password -t 10.0.0.1:sw1p1 --save-config

  # Two-step: (1) disable only  (2) after checks, save startup config only
  %(prog)s -u admin -p password -t 10.0.0.1:sw1p1
  %(prog)s -u admin -p password --save-only -t 10.0.0.1
  # Same as --save-only:
  %(prog)s -u admin -p password --save-config-only -f targets.json
""",
    )
    parser.add_argument(
        "-u", "--username", required=True, help="Username for switch authentication"
    )
    parser.add_argument(
        "-p", "--password", default=None, help="Password (will prompt if omitted)"
    )
    parser.add_argument(
        "-t",
        "--target",
        action="append",
        default=[],
        metavar="IP:port1,port2",
        help="Switch target: IP:port1,port2,... (repeatable). With --save-only / "
        "--save-config-only, IP alone is allowed (no port list).",
    )
    parser.add_argument(
        "-f",
        "--file",
        default=None,
        metavar="FILE",
        help="JSON file with targets; optional per-IP 'credentials' object inside the file",
    )
    parser.add_argument(
        "-c",
        "--credentials-file",
        default=None,
        metavar="FILE",
        help="JSON file mapping each switch IP to {\"password\": \"...\"} and optionally "
        '"username"; omitted keys use -u / -p. Overrides embedded credentials for the same IP.',
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="CSV_FILE",
        help="Output CSV file path (default: port_disable_report_<timestamp>.csv)",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=443,
        help="NVUE REST API port (default: 443)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Max concurrent switch connections (default: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help="After at least one successful port disable on a switch, persist applied "
        "configuration to startup via PATCH /revision/applied (like nv config save)",
    )
    parser.add_argument(
        "--save-only",
        "--save-config-only",
        action="store_true",
        dest="save_only",
        help="Do not disable ports; only save applied configuration to startup on each "
        "listed switch (same REST operation as nv config save). For a two-step workflow, "
        "run a normal disable first (without --save-config), then run again with "
        "--save-only (or --save-config-only) and the same switch list (-f targets.json "
        "or repeat -t IP per switch).",
    )

    args = parser.parse_args()

    if args.save_config and args.save_only:
        parser.error(
            "--save-config and --save-only/--save-config-only cannot be used together"
        )

    if not args.target and not args.file:
        parser.error("At least one --target or --file is required")

    password = args.password
    if password is None:
        password = getpass.getpass(f"Password for {args.username}: ")

    targets: dict[str, list[str]] = {}
    cred_maps: list[dict[str, dict[str, str]]] = []
    if args.file:
        file_targets, embedded_creds = load_targets_from_file(args.file)
        targets.update(file_targets)
        cred_maps.append(embedded_creds)
    if args.credentials_file:
        cred_maps.append(load_credentials_file(args.credentials_file))
    per_ip_credentials = merge_per_ip_credentials(*cred_maps) if cred_maps else {}
    if args.target:
        for ip, ports in parse_targets(
            args.target, allow_ip_only=args.save_only
        ).items():
            targets.setdefault(ip, []).extend(ports)

    if not targets:
        print("ERROR: No targets specified")
        sys.exit(1)

    total_ports = sum(len(p) for p in targets.values())
    if not args.save_only and total_ports == 0:
        print("ERROR: No ports specified (use IP:port1,port2,... for each --target)")
        sys.exit(1)

    print("\nNVOS Port Disable Tool")
    if args.save_only:
        print("Mode: save applied configuration to startup only (--save-only)")
        print(f"Switches: {len(targets)}")
    else:
        print(f"Switches: {len(targets)}  |  Ports to disable: {total_ports}")
    print(f"API port: {args.api_port}")
    if args.save_config:
        print("Save to startup: yes (after successful disables per switch)")
    print()

    if args.dry_run:
        if args.save_only:
            print("[DRY RUN] Would save applied configuration to startup on:")
            for ip in sorted(targets):
                print(f"  {ip}")
        else:
            print("[DRY RUN] Would disable the following ports:")
            for ip, ports in sorted(targets.items()):
                for port in sorted(ports):
                    print(f"  {ip} -> {port}")
            if args.save_config:
                print(
                    "\n[DRY RUN] Would save applied configuration to startup on each "
                    "switch that had at least one successful disable."
                )
        print("\nNo changes made.")
        sys.exit(0)

    all_results: list[PortResult] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        if args.save_only:
            futures = {
                executor.submit(
                    process_switch_save_only,
                    ip,
                    *resolve_switch_auth(ip, args.username, password, per_ip_credentials),
                    args.api_port,
                ): ip
                for ip in targets
            }
        else:
            futures = {
                executor.submit(
                    process_switch,
                    ip,
                    ports,
                    *resolve_switch_auth(ip, args.username, password, per_ip_credentials),
                    args.api_port,
                    args.save_config,
                ): ip
                for ip, ports in targets.items()
            }

        for future in as_completed(futures):
            ip = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as e:
                all_results.append(
                    PortResult(
                        switch_ip=ip,
                        port="*",
                        previous_state="unknown",
                        action="save" if args.save_only else "disable",
                        result="FAILED",
                        error=str(e),
                    )
                )

    print_report(all_results)

    output_file = args.output or (
        f"port_disable_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    save_csv(all_results, output_file)
    print(f"Report saved to: {output_file}")


if __name__ == "__main__":
    main()
