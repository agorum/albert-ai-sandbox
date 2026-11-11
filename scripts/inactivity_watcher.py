#!/usr/bin/env python3
"""Periodic housekeeping for ALBERT sandboxes (run by a systemd timer).

Stops idle sandboxes, removes expired ones and cleans up leftovers. See
``albert_sandbox.watcher`` for details.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from albert_sandbox.watcher import run_once  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_once())
