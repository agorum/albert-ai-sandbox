#!/usr/bin/env python3
"""ALBERT | AI Sandbox manager REST service (client API + admin web UI).

Entry point kept under its historic name so existing systemd units keep
working. The implementation lives in the ``albert_sandbox`` package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from albert_sandbox.web import serve  # noqa: E402

if __name__ == "__main__":
    serve()
