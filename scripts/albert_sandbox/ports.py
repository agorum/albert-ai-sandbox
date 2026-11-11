"""Host port allocation for sandbox services.

Ports come from one pool (``ALBERT_PORT_RANGE``, default 20000-29999). A port
is free if no sandbox in the database holds it (running or stopped) and no
process on the host is listening on it. Must be called under the host lock.
"""
from __future__ import annotations

import sqlite3
import subprocess
from typing import Iterable, List, Set

from .config import settings

PORT_COLUMNS = ("novnc_port", "vnc_port", "mcphub_port", "filesvc_port")


class PortsExhausted(RuntimeError):
    pass


def listening_ports() -> Set[int]:
    try:
        proc = subprocess.run(["ss", "-Htlnu"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    ports: Set[int] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[4]
        try:
            ports.add(int(local.rsplit(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return ports


def reserved_ports(conn: sqlite3.Connection) -> Set[int]:
    used: Set[int] = set()
    cols = ", ".join(PORT_COLUMNS)
    for row in conn.execute(f"SELECT {cols} FROM sandboxes"):
        for value in row:
            if value:
                used.add(int(value))
    return used


def allocate(conn: sqlite3.Connection, count: int, extra_used: Iterable[int] = ()) -> List[int]:
    lo, hi = settings().port_range
    used = reserved_ports(conn) | listening_ports() | set(extra_used)
    result: List[int] = []
    for port in range(lo, hi + 1):
        if port in used:
            continue
        result.append(port)
        if len(result) == count:
            return result
    raise PortsExhausted(f"no free ports left in range {lo}-{hi}")
