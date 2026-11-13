"""Runtime configuration.

Values are resolved in this order (first wins):
  1. process environment (e.g. systemd ``Environment=`` lines)
  2. ``<install dir>/config/albert.env`` (KEY=VALUE lines, written by install.sh
     and setup-tls.sh, editable by operators)
  3. built-in defaults below

All settings are read once into a frozen ``Settings`` object. Tests call
``configure(**overrides)`` to swap the active settings.
"""
from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Optional, Tuple

DEFAULT_INSTALL_DIR = "/opt/albert-ai-sandbox-manager"


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values[key] = value
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return values


# Destinations sandboxes must not reach: private/internal networks, carrier-grade
# NAT, link-local (incl. cloud metadata 169.254.169.254) and benchmark ranges.
DEFAULT_BLOCK_NETS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,169.254.0.0/16,198.18.0.0/15"


def _split_nets(value: str) -> Tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").replace(";", ",").split(",") if v.strip())


def _parse_range(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    try:
        lo, hi = value.split("-", 1)
        lo_i, hi_i = int(lo), int(hi)
        if 1024 <= lo_i < hi_i <= 65535:
            return lo_i, hi_i
    except (ValueError, AttributeError):
        pass
    return fallback


@dataclass(frozen=True)
class Settings:
    install_dir: Path
    db_path: Path
    data_dir: Path
    legacy_registry: Path
    lock_file: Path
    secret_key_file: Path
    env_file: Path
    docker_image: str
    docker_build_dir: Path
    storage_dir: Path
    nginx_dir: Path
    nginx_sandbox_dir: Path
    nginx_reload: bool
    port_range: Tuple[int, int]
    default_cpus: float
    default_memory_mb: int
    default_disk_gb: int
    min_memory_mb: int
    max_disk_gb: int
    pids_limit: int
    shm_max_mb: int
    inactivity_seconds: int
    max_age_seconds: int
    orphan_grace_seconds: int
    nginx_access_log: Path
    mcphub_timeout: int
    mcphub_poll_interval: float
    mcphub_settle_seconds: float
    public_url: str
    bind_host: str
    port: int
    admin_session_hours: int
    network_name: str = "albert-sandbox"
    network_subnet: str = "172.30.0.0/16"
    network_bridge: str = "br-albert"
    # Sandboxes of clients without internet access (created on first use).
    offline_network_name: str = "albert-sandbox-offline"
    offline_network_subnet: str = "172.31.0.0/16"
    offline_network_bridge: str = "br-albert-off"
    # DNS server given to offline sandboxes: a loopback address nothing listens
    # on, so name lookups fail at once (Docker's embedded DNS would otherwise
    # resolve external names for them from the host's network namespace).
    offline_dns: str = "127.0.0.254"
    profile_image_prefix: str = "albert-ai-sandbox-profile"
    profile_build_timeout: int = 3600
    firewall: bool = True
    block_nets: Tuple[str, ...] = ()
    allow_nets: Tuple[str, ...] = ()
    block_host_access: bool = True
    # Desktop access: one-time links and the desktop session they open.
    desktop_ticket_seconds: int = 120
    desktop_session_hours: int = 8
    # Admin UI on its own nginx server (port); see access.py.
    admin_port: int = 8443
    # nginx site written by the manager (main server, HTTPS, admin port).
    nginx_site_file: Path = Path("/etc/nginx/sites-available/albert-sandbox")
    nginx_site_link: Path = Path("/etc/nginx/sites-enabled/albert-sandbox")
    # noVNC client files, copied from the sandbox image and served by nginx.
    novnc_dir: Path = Path("/opt/albert-ai-sandbox-manager/novnc")
    # HTTPS: webroot for Let's Encrypt challenges, uploaded certificates.
    acme_dir: Path = Path("/var/www/albert-acme")
    tls_dir: Path = Path("/etc/albert-sandbox/tls")
    letsencrypt_dir: Path = Path("/etc/letsencrypt")
    certbot: str = "certbot"
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def disks_dir(self) -> Path:
        return self.storage_dir / "disks"

    @property
    def mounts_dir(self) -> Path:
        return self.storage_dir / "mnt"

    @property
    def skel_dir(self) -> Path:
        return self.storage_dir / "skel"

    @property
    def profiles_dir(self) -> Path:
        """Build contexts and build logs of sandbox profiles."""
        return self.data_dir / "profiles"


def _primary_ip() -> str:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        if out:
            return out[0]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def load_settings(overrides: Optional[Dict[str, str]] = None) -> Settings:
    install_dir = Path(os.environ.get("ALBERT_INSTALL_DIR", DEFAULT_INSTALL_DIR))
    env_file = Path(os.environ.get("ALBERT_ENV_FILE", str(install_dir / "config" / "albert.env")))
    merged: Dict[str, str] = _read_env_file(env_file)
    merged.update({k: v for k, v in os.environ.items() if k.startswith(("ALBERT_", "MANAGER_"))})
    if overrides:
        merged.update({k: str(v) for k, v in overrides.items()})

    def get(key: str, default: str) -> str:
        value = merged.get(key)
        return default if value is None or value == "" else value

    def get_int(key: str, default: int) -> int:
        try:
            return int(float(get(key, str(default))))
        except ValueError:
            return default

    def get_float(key: str, default: float) -> float:
        try:
            return float(get(key, str(default)))
        except ValueError:
            return default

    data_dir = Path(get("MANAGER_DATA_ROOT", str(install_dir / "data")))
    public_url = get("ALBERT_PUBLIC_URL", "")
    if not public_url:
        public_url = f"http://{_primary_ip()}"
    nginx_dir = Path(get("ALBERT_NGINX_DIR", "/etc/nginx/albert"))

    return Settings(
        install_dir=install_dir,
        db_path=Path(get("MANAGER_DB_PATH", str(data_dir / "manager.db"))),
        data_dir=data_dir,
        legacy_registry=Path(get("ALBERT_REGISTRY_FILE", str(install_dir / "config" / "container-registry.json"))),
        lock_file=Path(get("ALBERT_LOCK_FILE", str(data_dir / ".manager.lock"))),
        secret_key_file=Path(get("ALBERT_SECRET_KEY_FILE", str(data_dir / "secret_key"))),
        env_file=env_file,
        docker_image=get("ALBERT_DOCKER_IMAGE", "albert-ai-sandbox:latest"),
        docker_build_dir=Path(get("ALBERT_DOCKER_BUILD_DIR", str(install_dir / "docker"))),
        storage_dir=Path(get("ALBERT_STORAGE_DIR", "/var/lib/albert-sandbox")),
        nginx_dir=nginx_dir,
        nginx_sandbox_dir=Path(get("ALBERT_NGINX_SANDBOX_DIR", str(nginx_dir / "sandboxes"))),
        nginx_reload=get("ALBERT_NGINX_RELOAD", "1") not in ("0", "false", "no"),
        port_range=_parse_range(get("ALBERT_PORT_RANGE", "20000-29999"), (20000, 29999)),
        default_cpus=get_float("ALBERT_DEFAULT_CPUS", 1.0),
        default_memory_mb=get_int("ALBERT_DEFAULT_MEMORY_MB", 2048),
        default_disk_gb=get_int("ALBERT_DEFAULT_DISK_GB", 10),
        min_memory_mb=get_int("ALBERT_MIN_MEMORY_MB", 512),
        max_disk_gb=get_int("ALBERT_MAX_DISK_GB", 1000),
        pids_limit=get_int("ALBERT_PIDS_LIMIT", 4096),
        shm_max_mb=get_int("ALBERT_SHM_MAX_MB", 2048),
        inactivity_seconds=get_int("ALBERT_INACTIVITY_SECONDS", 600),
        max_age_seconds=get_int("ALBERT_MAX_AGE_SECONDS", 86400),
        orphan_grace_seconds=get_int("ALBERT_ORPHAN_GRACE_SECONDS", 900),
        nginx_access_log=Path(get("ALBERT_NGINX_ACCESS_LOG", "/var/log/nginx/access.log")),
        mcphub_timeout=get_int("ALBERT_MCPHUB_TIMEOUT", 120),
        mcphub_poll_interval=get_float("ALBERT_MCPHUB_POLL_INTERVAL", 2.0),
        mcphub_settle_seconds=get_float("ALBERT_MCPHUB_SETTLE_SECONDS", 5.0),
        public_url=public_url.rstrip("/"),
        bind_host=get("MANAGER_BIND_HOST", "127.0.0.1"),
        port=get_int("MANAGER_PORT", 5001),
        admin_session_hours=get_int("ALBERT_ADMIN_SESSION_HOURS", 12),
        network_name=get("ALBERT_NETWORK", "albert-sandbox"),
        network_subnet=get("ALBERT_NETWORK_SUBNET", "172.30.0.0/16"),
        network_bridge=get("ALBERT_NETWORK_BRIDGE", "br-albert")[:15],
        offline_network_name=get("ALBERT_OFFLINE_NETWORK", "albert-sandbox-offline"),
        offline_network_subnet=get("ALBERT_OFFLINE_SUBNET", "172.31.0.0/16"),
        offline_network_bridge=get("ALBERT_OFFLINE_BRIDGE", "br-albert-off")[:15],
        offline_dns=get("ALBERT_OFFLINE_DNS", "127.0.0.254"),
        profile_image_prefix=get("ALBERT_PROFILE_IMAGE_PREFIX", "albert-ai-sandbox-profile"),
        profile_build_timeout=get_int("ALBERT_PROFILE_BUILD_TIMEOUT", 3600),
        firewall=get("ALBERT_FIREWALL", "1") not in ("0", "false", "no"),
        block_nets=_split_nets(get("ALBERT_BLOCK_NETS", DEFAULT_BLOCK_NETS)),
        allow_nets=_split_nets(get("ALBERT_ALLOW_NETS", "")),
        block_host_access=get("ALBERT_BLOCK_HOST_ACCESS", "1") not in ("0", "false", "no"),
        desktop_ticket_seconds=max(10, get_int("ALBERT_DESKTOP_LINK_SECONDS", 120)),
        desktop_session_hours=max(1, get_int("ALBERT_DESKTOP_SESSION_HOURS", 8)),
        admin_port=get_int("ALBERT_ADMIN_PORT", 8443),
        nginx_site_file=Path(get("ALBERT_NGINX_SITE_FILE", "/etc/nginx/sites-available/albert-sandbox")),
        nginx_site_link=Path(get("ALBERT_NGINX_SITE_LINK", "/etc/nginx/sites-enabled/albert-sandbox")),
        novnc_dir=Path(get("ALBERT_NOVNC_DIR", str(install_dir / "novnc"))),
        acme_dir=Path(get("ALBERT_ACME_DIR", "/var/www/albert-acme")),
        tls_dir=Path(get("ALBERT_TLS_DIR", "/etc/albert-sandbox/tls")),
        letsencrypt_dir=Path(get("ALBERT_LETSENCRYPT_DIR", "/etc/letsencrypt")),
        certbot=get("ALBERT_CERTBOT", "certbot"),
        extra=merged,
    )


_settings: Optional[Settings] = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def configure(**overrides: object) -> Settings:
    """Replace the active settings (used by tests and the CLI)."""
    global _settings
    base = settings()
    _settings = replace(base, **overrides)  # type: ignore[arg-type]
    return _settings


def reset() -> None:
    global _settings
    _settings = None
