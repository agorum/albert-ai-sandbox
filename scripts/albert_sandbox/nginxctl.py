"""nginx configuration written by the manager.

Per-sandbox routes
    Each sandbox gets exactly one file ``<nginx_sandbox_dir>/<name>.conf``.
    The directory is owned by the manager: every file in it that does not
    belong to a sandbox in the database is removed by the reconcile step, so
    routes of deleted sandboxes can never point at ports that were reassigned
    to someone else. There is deliberately no global ``/mcphub/`` route.

    Every request to a sandbox service is checked by the manager first
    (``auth_request``, see access.py). nginx then talks to the container with
    the sandbox's internal tokens; cookies of this host never reach a
    container. The noVNC client files come from the host (``novnc_dir``), not
    from the container, so a sandbox cannot put its own scripts on a page of
    this host.

Site
    ``nginx_site_file``: the main server on port 80 (and 443 once a
    certificate is active, see tlsctl.py) plus the admin UI on its own port
    (``admin_port``), limited to the allowed addresses (access.py).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .config import settings

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,80}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")
# Fixed MCP Hub key of containers created before the per-sandbox token.
LEGACY_MCPHUB_TOKEN = "albert"


class NginxError(RuntimeError):
    pass


def conf_path(name: str) -> Path:
    return settings().nginx_sandbox_dir / f"{name}.conf"


def _token(value: Optional[str], fallback: str = "") -> str:
    token = value or fallback
    if token and not TOKEN_RE.match(token):
        raise NginxError("token with characters nginx cannot take")
    return token


PROXY_COMMON = """    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Cookies of this host (admin session, desktop session) never reach a container.
    proxy_set_header Cookie "";
    proxy_read_timeout 86400;
    proxy_buffering off;"""


def render(row: Mapping[str, Any]) -> str:
    """Routes of one sandbox (row of the sandboxes table)."""
    s = settings()
    name = row["name"]
    if not NAME_RE.match(name):
        raise NginxError(f"invalid sandbox name for nginx: {name!r}")
    novnc_port = int(row["novnc_port"])
    mcphub_port = row["mcphub_port"]
    filesvc_port = row["filesvc_port"]
    novnc = str(s.novnc_dir).rstrip("/")
    manager = f"http://127.0.0.1:{int(s.port)}"
    parts = [
        f"# Managed by albert-ai-sandbox-manager - sandbox {name}. Do not edit.",
        "",
        "# Desktop entry: one-time link (?ticket=...) or desktop session -> noVNC page.",
        f"location = /{name}/ {{",
        f"    proxy_pass {manager}/internal/desktop/{name}/entry$is_args$args;",
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_set_header X-Forwarded-Proto $scheme;",
        "    proxy_set_header X-Forwarded-Host $host;",
        '    proxy_set_header X-Forwarded-Prefix "";',
        '    proxy_set_header X-Albert-Internal "1";',
        "}",
        "",
        f"location = /{name}/vnc.html {{",
        f"    auth_request /_albert_auth/desktop-page/{name};",
        "    error_page 401 = @albert_desktop_expired;",
        "    error_page 403 = @albert_sandbox_offline;",
        f"    alias {novnc}/vnc.html;",
        '    add_header Cache-Control "no-store" always;',
        "}",
        "",
        f"location /{name}/websockify {{",
        f"    auth_request /_albert_auth/desktop/{name};",
        f"    proxy_pass http://127.0.0.1:{novnc_port}/websockify;",
        "    proxy_set_header Upgrade $http_upgrade;",
        '    proxy_set_header Connection "upgrade";',
        PROXY_COMMON,
        "    error_page 502 504 = @albert_sandbox_offline;",
        "}",
        "",
        "# noVNC client files (the same for every sandbox, served from the host).",
        f"location /{name}/ {{",
        f"    alias {novnc}/;",
        "}",
    ]
    if mcphub_port:
        token = _token(row["mcphub_token"] if "mcphub_token" in row.keys() else None, LEGACY_MCPHUB_TOKEN)
        for suffix in ("mcp", "sse"):
            parts += [
                "",
                # Exact path: nothing else of MCP Hub (dashboard, its API) is reachable.
                f"location = /{name}/mcphub/{suffix} {{",
                f"    auth_request /_albert_auth/mcp/{name};",
                "    error_page 401 = @albert_auth_required;",
                "    error_page 403 = @albert_auth_forbidden;",
                f"    proxy_pass http://127.0.0.1:{int(mcphub_port)}/{suffix};",
                "    proxy_set_header Upgrade $http_upgrade;",
                '    proxy_set_header Connection "upgrade";',
                PROXY_COMMON,
                f'    proxy_set_header Authorization "Bearer {token}";',
                '    proxy_set_header X-Albert-User "";',
                "    proxy_request_buffering off;",
                "    proxy_cache off;",
                "}",
            ]
    if filesvc_port:
        token = _token(row["filesvc_token"])
        parts += [
            "",
            f"location /{name}/files/ {{",
            f"    auth_request /_albert_auth/files/{name};",
            "    error_page 401 = @albert_auth_required;",
            "    error_page 403 = @albert_auth_forbidden;",
            f"    proxy_pass http://127.0.0.1:{int(filesvc_port)}/;",
            PROXY_COMMON,
            (f'    proxy_set_header Authorization "Bearer {token}";' if token
             else '    proxy_set_header Authorization "";'),
            '    proxy_set_header X-Albert-User "";',
            "    client_max_body_size 0;",
            "    proxy_request_buffering off;",
            "    proxy_cache off;",
            "    # Files from a sandbox never run as a page of this host. (add_header here replaces",
            "    # the inherited headers, so the general security headers are repeated.)",
            "    proxy_hide_header Content-Security-Policy;",
            "    add_header Content-Security-Policy \"sandbox; default-src 'none'\" always;",
            '    add_header X-Content-Type-Options "nosniff" always;',
            '    add_header X-Frame-Options "DENY" always;',
            '    add_header Referrer-Policy "no-referrer" always;',
            '    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;',
            "}",
        ]
    return "\n".join(parts) + "\n"


def write(sandbox: Mapping) -> bool:
    """Write the route file for a sandbox row. Returns True if it changed."""
    return _write_file(conf_path(sandbox["name"]), render(sandbox), 0o640)


def _write_file(path: Path, content: str, mode: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
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


GENERATION_PATH = "/.albert-generation"
PROBE_URL = "http://127.0.0.1"


def generation_file() -> Path:
    """Answers GENERATION_PATH on port 80 with the number of the last reload
    (included by the site with a glob, so a missing file is fine)."""
    return settings().nginx_dir / "generation.conf"


def _write_generation() -> str:
    gen = str(time.time_ns())
    _write_file(generation_file(),
                "# Managed by albert-ai-sandbox-manager: generation of the last reload. Do not edit.\n"
                f"location = {GENERATION_PATH} {{\n    access_log off;\n    default_type text/plain;\n"
                f"    return 200 \"{gen}\";\n}}\n", 0o644)
    return gen


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None  # a redirect (HTTPS site before the first reload) means: cannot tell


_probe_opener = urllib.request.build_opener(_NoRedirect)


def _served_generation() -> Optional[str]:
    """Generation the nginx worker answering a fresh connection serves; None if not available."""
    try:
        with _probe_opener.open(urllib.request.Request(PROBE_URL + GENERATION_PATH,
                                                       headers={"Connection": "close"}), timeout=2) as resp:
            return resp.read().decode("ascii", "replace").strip()
    except urllib.error.HTTPError:
        return None  # site without the generation include (older site): cannot tell
    except (OSError, ValueError):
        return ""


def _wait_for_generation(gen: str, timeout: float = 5.0) -> bool:
    """``systemctl reload`` returns before nginx serves the new configuration, and
    old workers keep taking connections for a moment. Wait until fresh connections
    answer with the new generation a few times in a row, so a sandbox route works
    as soon as the API call that created it returns."""
    deadline = time.monotonic() + timeout
    hits = 0
    while time.monotonic() < deadline:
        served = _served_generation()
        if served is None:
            time.sleep(0.3)
            return False
        hits = hits + 1 if served == gen else 0
        if hits >= 3:
            return True
        time.sleep(0.03)
    return False


def reload() -> None:
    """Validate and reload nginx and wait until it serves the new configuration.
    No-op if reloads are disabled (tests)."""
    if not settings().nginx_reload:
        return
    gen = _write_generation()
    test()
    proc = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise NginxError(f"nginx reload failed: {proc.stderr.strip()}")
    _wait_for_generation(gen)


def test() -> None:
    if not settings().nginx_reload:
        return
    proc = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise NginxError(f"nginx -t failed: {proc.stderr.strip()}")


# --------------------------------------------------------------------------
# site: main server (HTTP, HTTPS) and admin server
# --------------------------------------------------------------------------

def _manager_location(s: Any) -> str:
    return f"""    # Client REST API (API key). Admin UI and internal endpoints are not reachable here.
    location ^~ /manager/internal/ {{
        return 404;
    }}
    location /manager/ {{
        limit_req zone=albert_manager burst=40 nodelay;
        proxy_pass http://127.0.0.1:{int(s.port)}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Prefix /manager;
        proxy_set_header X-Albert-Internal "";
        proxy_set_header X-Albert-Admin-Gate "";
        # Creating/starting a sandbox waits for MCP Hub (up to ~2 minutes).
        proxy_read_timeout 600;
        proxy_buffering off;
        proxy_request_buffering off;
        client_max_body_size 1m;
    }}

    # Checks of the manager before every request to a sandbox service.
    location ^~ /_albert_auth/ {{
        internal;
        proxy_pass http://127.0.0.1:{int(s.port)}/internal/auth/;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Prefix "";
        proxy_set_header X-Albert-Internal "1";
    }}"""


def render_site(tls: Optional[Dict[str, Any]] = None, admin_allow: Optional[List[str]] = None) -> str:
    """The nginx site of the host. ``tls``: active certificate (cert, key) or None."""
    s = settings()
    albert = str(s.nginx_dir).rstrip("/")
    acme = str(s.acme_dir).rstrip("/")
    tls = tls if tls and tls.get("active") and tls.get("cert") and tls.get("key") else None
    admin_scheme = "https" if tls else "http"
    common = "\n".join([
        f"    # Admin UI: own port ({s.admin_port}).",
        "    location ^~ /manager/admin {",
        f"        return 302 {admin_scheme}://$host:{int(s.admin_port)}/manager/admin/;",
        "    }",
        _manager_location(s),
        "",
        "    # Pages shown instead of nginx errors, then one file per sandbox.",
        f"    include {albert}/sandbox-pages.conf;",
        f"    include {albert}/sandboxes/*.conf;",
        "",
        "    location / {",
        "        return 404;",
        "    }",
    ])
    acme_location = "\n".join([
        "    # Let's Encrypt (HTTP-01 challenge).",
        "    location ^~ /.well-known/acme-challenge/ {",
        f"        root {acme};",
        "        default_type text/plain;",
        "    }",
    ])
    ssl_lines = ""
    if tls:
        ssl_lines = "\n".join([
            f"    ssl_certificate {tls['cert']};",
            f"    ssl_certificate_key {tls['key']};",
            "    ssl_protocols TLSv1.2 TLSv1.3;",
            "    ssl_prefer_server_ciphers off;",
            "    ssl_session_cache shared:albert_tls:10m;",
            "    ssl_session_timeout 1d;",
        ])
    out = [
        "# Managed by albert-ai-sandbox-manager (written on install and on HTTPS/admin changes). Do not edit.",
        "",
        "server {",
        "    listen 80 default_server;",
        "    listen [::]:80 default_server;",
        "    server_name _;",
        "    server_tokens off;",
        "    client_max_body_size 0;",
        acme_location,
        "    # Generation of the last reload (the manager waits until nginx serves it).",
        f"    include {albert}/generation*.conf;",
    ]
    if tls:
        https_required = ("'{\"error\":\"https_required\",\"message\":\"This host only accepts HTTPS: "
                          "use https:// in the sandbox server URL.\"}'")
        out += [
            "",
            "    # HTTPS only: API keys and files never travel unencrypted.",
            "    location ^~ /manager/admin {",
            f"        return 301 https://$host:{int(s.admin_port)}/manager/admin/;",
            "    }",
            "    location ^~ /manager/ {",
            "        default_type application/json;",
            f"        return 403 {https_required};",
            "    }",
            "    location ~ ^/[^/]+/(mcphub|files)/ {",
            "        default_type application/json;",
            f"        return 403 {https_required};",
            "    }",
            "    location / {",
            "        return 301 https://$host$request_uri;",
            "    }",
            "}",
            "",
            "server {",
            "    listen 443 ssl http2 default_server;",
            "    listen [::]:443 ssl http2 default_server;",
            "    server_name _;",
            "    server_tokens off;",
            "    client_max_body_size 0;",
            ssl_lines,
            common,
            "}",
        ]
    else:
        out += ["", common, "}"]
    allow_lines: List[str] = []
    if admin_allow:
        allow_lines = ["    # Allowed addresses (admin UI, Einstellungen); loopback = SSH tunnel.",
                       "    allow 127.0.0.1;", "    allow ::1;"]
        allow_lines += [f"    allow {net};" for net in admin_allow]
        allow_lines += ["    deny all;"]
    listen_ssl = " ssl http2" if tls else ""
    out += [
        "",
        f"# Admin UI (own port, own origin: pages of sandboxes can never act in it).",
        "server {",
        f"    listen {int(s.admin_port)}{listen_ssl};",
        f"    listen [::]:{int(s.admin_port)}{listen_ssl};",
        "    server_name _;",
        "    server_tokens off;",
    ]
    if tls:
        out.append(ssl_lines)
    out += allow_lines
    out += [
        "    location = / {",
        "        return 302 /manager/admin/;",
        "    }",
        "    location = /manager/admin {",
        "        return 302 /manager/admin/;",
        "    }",
        "    location ^~ /manager/admin/ {",
        "        limit_req zone=albert_manager burst=40 nodelay;",
        f"        proxy_pass http://127.0.0.1:{int(s.port)}/admin/;",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host $host;",
        "        proxy_set_header X-Real-IP $remote_addr;",
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;",
        "        proxy_set_header X-Forwarded-Host $host:$server_port;",
        "        proxy_set_header X-Forwarded-Prefix /manager;",
        '        proxy_set_header X-Albert-Admin-Gate "1";',
        '        proxy_set_header X-Albert-Internal "";',
        "        proxy_read_timeout 600;",
        "        client_max_body_size 1m;",
        "    }",
        "    location / {",
        "        return 404;",
        "    }",
        "}",
    ]
    return "\n".join(out) + "\n"


def write_site(tls: Optional[Dict[str, Any]] = None, admin_allow: Optional[List[str]] = None,
               reload_nginx: bool = True) -> bool:
    """Write the site, test it and reload nginx; restores the previous site if nginx refuses it."""
    s = settings()
    path = s.nginx_site_file
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    content = render_site(tls, admin_allow)
    if previous == content and s.nginx_site_link.exists():
        return False
    _write_file(path, content, 0o644)
    link = s.nginx_site_link
    if not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        link.symlink_to(path)
    if not reload_nginx:
        return True
    try:
        reload()
    except NginxError:
        if previous is not None:
            _write_file(path, previous, 0o644)
        else:
            # No previous site: take the refused one out again, or every later
            # reload (sandbox routes) would fail on it.
            path.unlink(missing_ok=True)
            if link.is_symlink() and not link.exists():
                link.unlink()
        try:
            reload()
        except NginxError:
            pass
        raise
    return True
