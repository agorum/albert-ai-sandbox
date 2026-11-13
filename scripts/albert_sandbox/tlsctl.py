"""HTTPS for the sandbox host: Let's Encrypt or an uploaded certificate.

The state lives in the database (kv ``tls``) and is turned into the nginx
site by ``apply_site`` (nginxctl.render_site). Every change is tested with
``nginx -t`` before nginx is reloaded; if nginx refuses it, the previous site
and certificate stay active.

* Let's Encrypt: ``certbot certonly --webroot`` (HTTP-01 on port 80, the site
  serves ``acme_dir`` for that in every mode). certbot's own timer renews the
  certificate; a deploy hook reloads nginx afterwards.
* Own certificate: PEM certificate (+ intermediate certificates) and private
  key. Checked before use: key matches, name matches, not expired. Stored
  root-only under ``tls_dir``; the key is never shown again.

Once HTTPS is active, port 80 only redirects browsers and refuses API, MCP and
file requests ("https_required"), so API keys never travel unencrypted.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import access, db, nginxctl
from .config import settings
from .errors import BadRequest, Conflict, SandboxError, Unavailable

KV_KEY = "tls"
FQDN_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
MAX_PEM = 64 * 1024
WARN_DAYS = 14


class TlsError(SandboxError):
    status = 400
    code = "tls_error"


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load(conn: sqlite3.Connection) -> Dict[str, Any]:
    state = db.kv_get(conn, KV_KEY, {}) or {}
    return state if isinstance(state, dict) else {}


def _save(conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
    db.kv_set(conn, KV_KEY, state)
    access.reset_url_cache()


def apply_site(reload_nginx: bool = True) -> bool:
    """Write the nginx site from the current state (HTTPS, admin addresses)."""
    with db.session() as conn:
        state = load(conn)
        allow = access.admin_allow(conn)
    return nginxctl.write_site(state if state.get("active") else None, allow, reload_nginx=reload_nginx)


# --------------------------------------------------------------------------
# certificate inspection (openssl)
# --------------------------------------------------------------------------

def _openssl(args: List[str], stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise Unavailable("openssl is not installed on this host")


def _date(text: str) -> Optional[float]:
    text = (text or "").strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def cert_info(path: Path) -> Dict[str, Any]:
    """Subject, issuer, validity and names of the first certificate in a PEM file."""
    proc = _openssl(["x509", "-in", str(path), "-noout", "-subject", "-issuer", "-startdate", "-enddate",
                     "-ext", "subjectAltName", "-nameopt", "RFC2253"])
    if proc.returncode != 0:
        raise TlsError("The certificate could not be read (PEM expected: -----BEGIN CERTIFICATE-----).",
                       code="tls_invalid_certificate")
    fields: Dict[str, str] = {}
    names: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        for key in ("subject", "issuer", "notBefore", "notAfter"):
            if line.startswith(key + "="):
                fields[key] = line.split("=", 1)[1].strip()
        for part in line.split(","):
            part = part.strip()
            if part.startswith("DNS:"):
                names.append(part[4:].strip().lower())
            elif part.startswith("IP Address:"):
                names.append(part[len("IP Address:"):].strip())
    not_after = _date(fields.get("notAfter", ""))
    not_before = _date(fields.get("notBefore", ""))
    cn = ""
    match = re.search(r"(?:^|,)CN=([^,]+)", fields.get("subject", ""))
    if match:
        cn = match.group(1)
        if not names:
            names.append(cn.lower())
    now = time.time()
    from .sandboxes import iso  # local import: sandboxes is a large module
    return {
        "subject": fields.get("subject", ""),
        "commonName": cn,
        "issuer": fields.get("issuer", ""),
        "names": names,
        "notBefore": iso(not_before) if not_before else None,
        "notAfter": iso(not_after) if not_after else None,
        "daysLeft": int((not_after - now) // 86400) if not_after else None,
        "expired": bool(not_after and not_after < now),
        "selfSigned": bool(fields.get("subject")) and fields.get("subject") == fields.get("issuer"),
    }


def name_matches(fqdn: str, names: List[str]) -> bool:
    fqdn = fqdn.lower().rstrip(".")
    for name in names:
        name = name.lower().rstrip(".")
        if name == fqdn:
            return True
        if name.startswith("*.") and "." in fqdn and fqdn.split(".", 1)[1] == name[2:]:
            return True
    return False


def clean_host(value: Any, allow_ip: bool = True) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        raise BadRequest("The host name is required (e.g. sandbox.example.com).", code="tls_host_required")
    try:
        ipaddress.ip_address(text)
        if allow_ip:
            return text
        raise BadRequest("Let's Encrypt needs a DNS name, not an IP address.", code="tls_invalid_host")
    except ValueError:
        pass
    if not FQDN_RE.match(text):
        raise BadRequest(f"'{text}' is not a valid host name (e.g. sandbox.example.com).", code="tls_invalid_host")
    return text


def _pem(value: Any, label: str, required: bool = True) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        if required:
            raise BadRequest(f"{label} is missing.", code="tls_missing")
        return ""
    if len(text) > MAX_PEM:
        raise BadRequest(f"{label} is too large (max 64 KB).", code="tls_too_large")
    if "-----BEGIN " not in text:
        raise BadRequest(f"{label} must be in PEM format (-----BEGIN ...-----).", code="tls_not_pem")
    return text + "\n"


def validate_custom(cert_pem: str, chain_pem: str, key_pem: str, host: str, workdir: Path) -> Dict[str, Any]:
    """Check an uploaded certificate; writes fullchain.pem / privkey.pem into workdir."""
    cert_pem = _pem(cert_pem, "The certificate")
    chain_pem = _pem(chain_pem, "The intermediate certificates", required=False)
    key_pem = _pem(key_pem, "The private key")
    if "PRIVATE KEY" in cert_pem or "PRIVATE KEY" in chain_pem:
        raise BadRequest("A private key was pasted into the certificate field.", code="tls_key_in_certificate")
    if "ENCRYPTED" in key_pem:
        raise BadRequest("The private key is protected by a passphrase; upload it without one.",
                         code="tls_key_encrypted")
    fullchain = workdir / "fullchain.pem"
    keyfile = workdir / "privkey.pem"
    fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key_pem)
    fullchain.write_text(cert_pem + chain_pem, encoding="utf-8")
    os.chmod(fullchain, 0o644)
    info = cert_info(fullchain)
    key_pub = _openssl(["pkey", "-in", str(keyfile), "-pubout", "-passin", "pass:"])
    if key_pub.returncode != 0:
        raise BadRequest("The private key could not be read (PEM expected, without passphrase).",
                         code="tls_invalid_key")
    cert_pub = _openssl(["x509", "-in", str(fullchain), "-noout", "-pubkey"])
    if cert_pub.returncode != 0 or cert_pub.stdout.strip() != key_pub.stdout.strip():
        raise BadRequest("The private key does not belong to the certificate.", code="tls_key_mismatch")
    if info["expired"]:
        raise BadRequest(f"The certificate expired on {info['notAfter']}.", code="tls_expired")
    if not name_matches(host, info["names"]):
        raise BadRequest(f"The certificate is not issued for {host} (it covers: "
                         f"{', '.join(info['names']) or '-'}).", code="tls_name_mismatch")
    return info


# --------------------------------------------------------------------------
# activation
# --------------------------------------------------------------------------

def _activate(conn: sqlite3.Connection, state: Dict[str, Any], actor: str, action: str) -> None:
    """Write the site with this state; keeps the previous one if nginx refuses it."""
    allow = access.admin_allow(conn)
    try:
        nginxctl.write_site(state if state.get("active") else None, allow)
    except nginxctl.NginxError as exc:
        raise TlsError(f"nginx refused the new configuration, the previous one stays active: {exc}",
                       code="tls_nginx_failed")
    _save(conn, state)
    db.audit(conn, actor, action, state.get("fqdn") or None,
             {"mode": state.get("mode"), "active": bool(state.get("active"))})


def install_custom(cert_pem: str, chain_pem: str, key_pem: str, host: Any, actor: str) -> Dict[str, Any]:
    s = settings()
    host = clean_host(host)
    s.tls_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(s.tls_dir, 0o700)
    target = s.tls_dir / f"custom-{int(time.time())}-{os.getpid()}"
    workdir = Path(tempfile.mkdtemp(prefix=".upload-", dir=str(s.tls_dir)))
    try:
        info = validate_custom(cert_pem, chain_pem, key_pem, host, workdir)
        workdir.rename(target)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    state = {"active": True, "mode": "custom", "fqdn": host, "cert": str(target / "fullchain.pem"),
             "key": str(target / "privkey.pem"), "activatedAt": int(time.time())}
    with db.session() as conn:
        previous = load(conn)
        try:
            _activate(conn, state, actor, "tls.custom")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
    _cleanup_custom(keep=[Path(state["cert"]).parent, Path(previous.get("cert", "") or "/").parent])
    return status()


def _cleanup_custom(keep: List[Path]) -> None:
    """Remove uploaded certificates no longer in use (the active and the previous one stay)."""
    tls_dir = settings().tls_dir
    if not tls_dir.exists():
        return
    keep_set = {p.resolve() for p in keep if str(p) not in ("", "/")}
    custom = sorted((p for p in tls_dir.glob("custom-*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for path in custom:
        if path.resolve() not in keep_set:
            shutil.rmtree(path, ignore_errors=True)


def disable(actor: str) -> Dict[str, Any]:
    """Back to plain HTTP (e.g. before moving the host to another name)."""
    with db.session() as conn:
        state = load(conn)
        if not state.get("active"):
            return status()
        state = {**state, "active": False}
        _activate(conn, state, actor, "tls.disable")
    return status()


# --------------------------------------------------------------------------
# Let's Encrypt
# --------------------------------------------------------------------------

def certbot_available() -> bool:
    return shutil.which(settings().certbot) is not None


def _deploy_hook() -> None:
    hooks = settings().letsencrypt_dir / "renewal-hooks" / "deploy"
    try:
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "albert-sandbox-nginx.sh"
        hook.write_text("#!/bin/sh\n# ALBERT Sandbox: load the renewed certificate.\nsystemctl reload nginx\n",
                        encoding="utf-8")
        os.chmod(hook, 0o755)
    except OSError:
        pass


def start_letsencrypt(host: Any, email: Any, agree: Any, actor: str) -> Dict[str, Any]:
    """Validate the request and mark a certificate job as running (run_letsencrypt does the work)."""
    host = clean_host(host, allow_ip=False)
    email = str(email or "").strip()
    if not EMAIL_RE.match(email):
        raise BadRequest("A valid e-mail address is required (Let's Encrypt sends expiry notices to it).",
                         code="tls_invalid_email")
    if agree is not True and str(agree).lower() not in ("1", "true", "yes", "on"):
        raise BadRequest("The terms of service of Let's Encrypt must be accepted.", code="tls_terms")
    if not certbot_available():
        raise Unavailable("certbot is not installed on this host (install.sh installs it).",
                          code="tls_certbot_missing")
    with db.session() as conn:
        state = load(conn)
        job = state.get("job") or {}
        if job.get("state") == "running" and time.time() - (job.get("startedAt") or 0) < 600:
            raise Conflict("A certificate request is already running.", code="tls_busy")
        state["job"] = {"state": "running", "host": host, "email": email, "startedAt": int(time.time()),
                        "actor": actor}
        _save(conn, state)
    return status()


def run_letsencrypt(actor: str) -> None:
    """Request the certificate of the running job and activate it (seconds to a minute)."""
    s = settings()
    with db.session() as conn:
        job = dict(load(conn).get("job") or {})
    host, email = job.get("host"), job.get("email")
    if not host or not email:
        return
    s.acme_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(s.acme_dir, 0o755)
    cmd = [s.certbot, "certonly", "--webroot", "-w", str(s.acme_dir), "-d", host, "-m", email,
           "--agree-tos", "--non-interactive", "--keep-until-expiring", "--no-eff-email"]
    error: Optional[str] = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-12:])
            error = f"certbot failed (exit {proc.returncode}): {tail}"
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"certbot could not run: {exc}"
    live = s.letsencrypt_dir / "live" / host
    cert, key = live / "fullchain.pem", live / "privkey.pem"
    if not error and not (cert.exists() and key.exists()):
        error = f"certbot reported success, but {cert} is missing"
    with db.session() as conn:
        state = load(conn)
        if error:
            state["job"] = {**job, "state": "failed", "finishedAt": int(time.time()), "error": error[:4000]}
            _save(conn, state)
            db.audit(conn, actor, "tls.letsencrypt_failed", host, {"error": error[:500]})
            return
        new_state = {"active": True, "mode": "letsencrypt", "fqdn": host, "email": email, "cert": str(cert),
                     "key": str(key), "activatedAt": int(time.time()),
                     "job": {**job, "state": "done", "finishedAt": int(time.time())}}
        try:
            _activate(conn, new_state, actor, "tls.letsencrypt")
        except TlsError as exc:
            state["job"] = {**job, "state": "failed", "finishedAt": int(time.time()), "error": exc.message}
            _save(conn, state)
            return
    _deploy_hook()


def import_existing() -> Optional[Dict[str, Any]]:
    """Take over a certificate set up by the former setup-tls.sh (certbot --nginx)."""
    s = settings()
    with db.session() as conn:
        if load(conn):
            return None
    url = s.public_url
    if not url.startswith("https://"):
        return None
    host = url[len("https://"):].split("/", 1)[0].split(":", 1)[0]
    live = s.letsencrypt_dir / "live" / host
    if not (live / "fullchain.pem").exists() or not (live / "privkey.pem").exists():
        return None
    state = {"active": True, "mode": "letsencrypt", "fqdn": host, "cert": str(live / "fullchain.pem"),
             "key": str(live / "privkey.pem"), "activatedAt": int(time.time()), "imported": True}
    with db.session() as conn:
        _save(conn, state)
        db.audit(conn, "system", "tls.import", host, {"mode": "letsencrypt"})
    _deploy_hook()
    return state


# --------------------------------------------------------------------------
# status for UI / CLI
# --------------------------------------------------------------------------

def status_brief() -> Dict[str, Any]:
    """Cheap HTTPS state (no certificate parsing), e.g. for /health."""
    with db.session() as conn:
        state = load(conn)
    return {"active": bool(state.get("active")), "host": state.get("fqdn") if state.get("active") else None}


def status() -> Dict[str, Any]:
    s = settings()
    with db.session() as conn:
        state = load(conn)
        allow = access.admin_allow(conn)
    active = bool(state.get("active"))
    info = None
    warnings: List[str] = []
    if state.get("cert") and Path(state["cert"]).exists():
        try:
            info = cert_info(Path(state["cert"]))
        except SandboxError as exc:
            warnings.append(exc.message)
    elif active:
        warnings.append(f"Certificate file {state.get('cert')} is missing.")
    if active and info:
        days = info.get("daysLeft")
        if info.get("expired"):
            warnings.append("The certificate has expired.")
        elif days is not None and days < WARN_DAYS:
            if state.get("mode") == "custom":
                warnings.append(f"The certificate expires in {days} days - upload a new one.")
            elif days < 10:
                warnings.append(f"The certificate expires in {days} days although it should have been renewed "
                                "(check: systemctl status certbot.timer).")
    if active:
        host = access.url_host(state.get("fqdn") or "")
    else:
        host = access.public_url().split("://", 1)[-1].split("/", 1)[0]
        host = host if host.startswith("[") else host.split(":", 1)[0]
    job = state.get("job") or None
    if job and job.get("state") == "running" and time.time() - (job.get("startedAt") or 0) > 600:
        job = {**job, "state": "failed", "error": "The request did not finish (interrupted)."}
    from .sandboxes import iso
    return {
        "active": active,
        "mode": state.get("mode") if state else None,
        "host": state.get("fqdn") or None,
        "email": state.get("email") or None,
        "activatedAt": iso(state.get("activatedAt")) if state.get("activatedAt") else None,
        "certificate": info,
        "warnings": warnings,
        "job": {k: v for k, v in (job or {}).items() if k != "email"} or None,
        "certbotAvailable": certbot_available(),
        "publicUrl": access.public_url(),
        "adminUrl": f"{'https' if active else 'http'}://{host}:{s.admin_port}/manager/admin/",
        "adminPort": s.admin_port,
        "adminAllow": allow,
    }
