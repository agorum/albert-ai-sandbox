"""Per-sandbox nginx routes.

Each sandbox gets exactly one file ``<nginx_sandbox_dir>/<name>.conf``. The
directory is owned by the manager: every file in it that does not belong to
a sandbox in the database is removed by the reconcile step, so routes of
deleted sandboxes can never point at ports that were reassigned to someone
else. There is deliberately no global ``/mcphub/`` route any more.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import List, Mapping, Optional

from .config import settings

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,80}$")


class NginxError(RuntimeError):
    pass


def conf_path(name: str) -> Path:
    return settings().nginx_sandbox_dir / f"{name}.conf"


def render(name: str, novnc_port: int, mcphub_port: Optional[int], filesvc_port: Optional[int],
           vnc_password: str) -> str:
    if not NAME_RE.match(name):
        raise NginxError(f"invalid sandbox name for nginx: {name!r}")
    pw = urllib.parse.quote(vnc_password or "", safe="")
    proxy_common = """    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;"""
    parts = [
        f"# Managed by albert-ai-sandbox-manager - sandbox {name}. Do not edit.",
        f"location = /{name}/ {{",
        "    # relative Location header: works behind TLS terminators / port mappings",
        "    absolute_redirect off;",
        f"    return 301 /{name}/vnc.html?path={name}/websockify&password={pw}&autoconnect=true&resize=scale;",
        "}",
        "",
        f"location /{name}/ {{",
        f"    proxy_pass http://127.0.0.1:{int(novnc_port)}/;",
        proxy_common,
        "}",
        "",
        f"location /{name}/websockify {{",
        f"    proxy_pass http://127.0.0.1:{int(novnc_port)}/websockify;",
        proxy_common,
        "}",
    ]
    if mcphub_port:
        for suffix in ("mcp", "sse"):
            parts += [
                "",
                f"location /{name}/mcphub/{suffix} {{",
                f"    proxy_pass http://127.0.0.1:{int(mcphub_port)}/{suffix};",
                proxy_common,
                "    proxy_request_buffering off;",
                "    proxy_cache off;",
                "}",
            ]
    if filesvc_port:
        parts += [
            "",
            f"location /{name}/files/ {{",
            f"    proxy_pass http://127.0.0.1:{int(filesvc_port)}/;",
            proxy_common,
            "    client_max_body_size 0;",
            "    proxy_request_buffering off;",
            "    proxy_cache off;",
            "}",
        ]
    return "\n".join(parts) + "\n"


def write(sandbox: Mapping) -> bool:
    """Write the route file for a sandbox row. Returns True if it changed."""
    name = sandbox["name"]
    content = render(name, sandbox["novnc_port"], sandbox["mcphub_port"], sandbox["filesvc_port"],
                     sandbox["vnc_password"] or "")
    path = conf_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return True


def remove(name: str) -> bool:
    path = conf_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def list_names() -> List[str]:
    directory = settings().nginx_sandbox_dir
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.conf"))


def stray_files() -> List[Path]:
    """Leftover temp files or non-conf files in the managed directory."""
    directory = settings().nginx_sandbox_dir
    if not directory.exists():
        return []
    return [p for p in directory.iterdir() if p.is_file() and p.suffix != ".conf"]


def reload() -> None:
    """Validate and reload nginx. No-op if reloads are disabled (tests)."""
    if not settings().nginx_reload:
        return
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=60)
    if test.returncode != 0:
        raise NginxError(f"nginx -t failed: {test.stderr.strip()}")
    proc = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise NginxError(f"nginx reload failed: {proc.stderr.strip()}")
