#!/usr/bin/env python3
"""
Generate targets.json for nvos_port_disable.

Maps fabric notation R/C to NVUE names swRpC (e.g. 37/1 -> sw37p1).
By default writes rows 37..144 with columns 1..2 (37/1 through 144/2).

If ./switches exists, each listed IP gets the same port list; otherwise use
--ip only. Use --single-ip to force one host even when ./switches exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_switch_ips(path: Path) -> list[str]:
    text = path.read_text()
    ips: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ips.append(line)
    return ips


def ports_for_range(
    row_start: int, row_end: int, col_start: int, col_end: int
) -> list[str]:
    ports: list[str] = []
    for r in range(row_start, row_end + 1):
        for c in range(col_start, col_end + 1):
            ports.append(f"sw{r}p{c}")
    return ports


def parse_row_ranges(spec: str) -> list[tuple[int, int]]:
    """
    Parse '1-16,25-144' into [(1, 16), (25, 144)] (inclusive row indices).
    """
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"Invalid row span (expected R1-R2): {part!r}")
        a, b = part.split("-", 1)
        row_start, row_end = int(a.strip()), int(b.strip())
        if row_start > row_end:
            raise ValueError(f"Row start > end in {part!r}")
        ranges.append((row_start, row_end))
    if not ranges:
        raise ValueError("No row ranges in spec")
    return ranges


def ports_for_row_ranges(
    ranges: list[tuple[int, int]], col_start: int, col_end: int
) -> list[str]:
    ports: list[str] = []
    for row_start, row_end in ranges:
        ports.extend(ports_for_range(row_start, row_end, col_start, col_end))
    return ports


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate targets.json (R/C grid -> swRpC names)."
    )
    parser.add_argument("--row-start", type=int, default=37)
    parser.add_argument("--row-end", type=int, default=144)
    parser.add_argument(
        "--row-ranges",
        metavar="SPEC",
        help=(
            "Comma-separated fabric row spans R1-R2 (e.g. 1-16,25-144 for "
            "1/1-16/2 and 25/1-144/2 with cols 1-2). Overrides --row-start/--row-end."
        ),
    )
    parser.add_argument("--col-start", type=int, default=1)
    parser.add_argument("--col-end", type=int, default=2)
    parser.add_argument(
        "--ip",
        default="10.0.0.1",
        help="Single switch IP (used only if --switches file is missing)",
    )
    parser.add_argument(
        "--single-ip",
        action="store_true",
        help="Ignore --switches; emit only --ip (for testing)",
    )
    parser.add_argument(
        "--switches",
        type=Path,
        default=Path("switches"),
        help="File with one switch IP per line (# comments and blank lines ok)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("targets.json"),
    )
    args = parser.parse_args()

    try:
        if args.row_ranges:
            ranges = parse_row_ranges(args.row_ranges)
            ports = ports_for_row_ranges(ranges, args.col_start, args.col_end)
        else:
            ports = ports_for_range(
                args.row_start, args.row_end, args.col_start, args.col_end
            )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.switches.exists() and not args.single_ip:
        ips = load_switch_ips(args.switches)
        if not ips:
            print(f"ERROR: No IPs found in {args.switches}", file=sys.stderr)
            sys.exit(1)
        targets = {ip: ports.copy() for ip in ips}
        print(
            f"Switches file: {args.switches} ({len(ips)} IPs) × {len(ports)} ports each"
        )
    else:
        targets = {args.ip: ports}
        if args.single_ip:
            print(f"Single IP {args.ip} (--single-ip)")
        else:
            print(f"Single IP {args.ip} ({args.switches} not found)")

    doc = {"targets": targets}
    args.output.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
