"""Network isolation of sandboxes.

All sandboxes live in one dedicated Docker bridge network whose
inter-container communication is disabled (``enable_icc=false``): a sandbox
cannot reach any other sandbox, not even its MCP Hub or file service.

Two iptables chains add egress rules for traffic coming from that network:

* ``ALBERT-SANDBOX`` (hooked into Docker's ``DOCKER-USER`` chain, i.e. routed
  traffic): drops new connections to private/internal networks
  (``ALBERT_BLOCK_NETS``: RFC 1918, CGNAT, link-local incl. cloud metadata, ...)
  except explicitly allowed ones (``ALBERT_ALLOW_NETS``). The internet stays
  reachable.
* ``ALBERT-SANDBOX-IN`` (hooked into ``INPUT``): drops new connections from
  sandboxes to services on the host itself (ssh, nginx, ...). Replies to
  connections the host opened (nginx -> sandbox ports) are allowed.

Rules are applied idempotently by the manager before every create/start, by
the watcher every minute (self-healing) and at boot by
``albert-sandbox-firewall.service``. A comment rule carrying a hash of the
configuration detects outdated rule sets.
"""
from __future__ import annotations

import hashlib
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import dockerctl
from .config import settings
from .errors import SandboxError

CHAIN_FWD = "ALBERT-SANDBOX"
CHAIN_IN = "ALBERT-SANDBOX-IN"


class NetworkError(SandboxError):
    code = "network"


# --------------------------------------------------------------------------
# docker network
# --------------------------------------------------------------------------

def ensure_network() -> Dict[str, Any]:
    """Create the sandbox network if missing and verify its isolation settings."""
    s = settings()
    info = dockerctl.network_inspect(s.network_name)
    if info is None:
        dockerctl.network_create(s.network_name, s.network_subnet, s.network_bridge,
                                 {dockerctl.LABEL_MANAGER: "1"})
        info = dockerctl.network_inspect(s.network_name)
        if info is None:
            raise NetworkError(f"could not create docker network {s.network_name}")
    options = info.get("Options") or {}
    if options.get("com.docker.network.bridge.enable_icc", "true") != "false":
        raise NetworkError(
            f"docker network {s.network_name} allows inter-container traffic; "
            "remove it (docker network rm) so the manager can recreate it isolated")
    return info


def network_subnet(info: Optional[Dict[str, Any]] = None) -> str:
    info = info or dockerctl.network_inspect(settings().network_name) or {}
    for cfg in ((info.get("IPAM") or {}).get("Config") or []):
        if cfg.get("Subnet") and ":" not in cfg["Subnet"]:
            return cfg["Subnet"]
    return settings().network_subnet


def container_networks(info: Optional[Dict[str, Any]]) -> List[str]:
    return sorted(((info or {}).get("NetworkSettings") or {}).get("Networks", {}).keys())


def is_isolated(info: Optional[Dict[str, Any]]) -> bool:
    return container_networks(info) == [settings().network_name]


def isolate(name: str, info: Optional[Dict[str, Any]] = None) -> bool:
    """Move a container into the sandbox network (works while it runs).

    Published ports stay as they are; returns True if something changed.
    """
    info = info or dockerctl.inspect_one(name)
    if info is None:
        return False
    target = settings().network_name
    nets = container_networks(info)
    if nets == [target]:
        return False
    if target not in nets:
        dockerctl.network_connect(target, name)
    for other in nets:
        if other != target:
            dockerctl.network_disconnect(other, name)
    return True


# --------------------------------------------------------------------------
# firewall
# --------------------------------------------------------------------------

def _iptables(args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["iptables", "-w", "10", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise NetworkError("iptables not found - cannot isolate sandboxes") from exc
    if check and proc.returncode != 0:
        raise NetworkError(f"iptables {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def desired_rules(subnet: str) -> Tuple[List[List[str]], List[List[str]], str]:
    """Rule specs for both chains plus a fingerprint of the configuration."""
    s = settings()
    fwd: List[List[str]] = [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"]]
    for net in s.allow_nets:
        fwd.append(["-s", subnet, "-d", net, "-j", "RETURN"])
    for net in s.block_nets:
        fwd.append(["-s", subnet, "-d", net, "-j", "DROP"])
    inp: List[List[str]] = [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"]]
    if s.block_host_access:
        inp.append(["-j", "DROP"])
    fingerprint = hashlib.sha256(repr((subnet, fwd, inp)).encode()).hexdigest()[:16]
    return fwd, inp, fingerprint


def _marker(fingerprint: str) -> List[str]:
    """No-op rule (source 255.255.255.255 never occurs) carrying the config fingerprint."""
    return ["-s", "255.255.255.255/32", "-m", "comment", "--comment", f"albert-sandbox {fingerprint}",
            "-j", "RETURN"]


def firewall_status() -> Dict[str, Any]:
    s = settings()
    if not s.firewall:
        return {"enabled": False, "ok": True}
    subnet = network_subnet()
    _, _, fingerprint = desired_rules(subnet)
    checks = {
        "forwardChain": _iptables(["-C", CHAIN_FWD, *_marker(fingerprint)], check=False).returncode == 0,
        "inputChain": _iptables(["-C", CHAIN_IN, *_marker(fingerprint)], check=False).returncode == 0,
        "dockerUserJump": _iptables(["-C", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode == 0,
        "inputJump": _iptables(["-C", "INPUT", "-s", subnet, "-j", CHAIN_IN], check=False).returncode == 0,
    }
    return {"enabled": True, "ok": all(checks.values()), "subnet": subnet, "fingerprint": fingerprint, **checks}


def apply_firewall() -> Dict[str, Any]:
    """(Re)build both chains and make sure they are hooked in. Idempotent."""
    s = settings()
    if not s.firewall:
        return {"enabled": False, "applied": False}
    subnet = network_subnet()
    fwd, inp, fingerprint = desired_rules(subnet)
    for chain, rules in ((CHAIN_FWD, fwd), (CHAIN_IN, inp)):
        if _iptables(["-n", "-L", chain], check=False).returncode != 0:
            _iptables(["-N", chain])
        _iptables(["-F", chain])
        _iptables(["-A", chain, *_marker(fingerprint)])
        for rule in rules:
            _iptables(["-A", chain, *rule])
    # DOCKER-USER exists once dockerd ran; create it otherwise (dockerd keeps it).
    if _iptables(["-n", "-L", "DOCKER-USER"], check=False).returncode != 0:
        _iptables(["-N", "DOCKER-USER"])
    if _iptables(["-C", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode != 0:
        _iptables(["-I", "DOCKER-USER", "1", "-j", CHAIN_FWD])
    if _iptables(["-C", "INPUT", "-s", subnet, "-j", CHAIN_IN], check=False).returncode != 0:
        _iptables(["-I", "INPUT", "1", "-s", subnet, "-j", CHAIN_IN])
    return {"enabled": True, "applied": True, "subnet": subnet, "fingerprint": fingerprint}


def ensure_firewall() -> bool:
    """Apply the rules if they are missing or outdated. Returns True if applied."""
    if not settings().firewall:
        return False
    if firewall_status()["ok"]:
        return False
    apply_firewall()
    return True


def remove_firewall() -> None:
    """Unhook and delete both chains (uninstall)."""
    subnet = network_subnet()
    while _iptables(["-D", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode == 0:
        pass
    while _iptables(["-D", "INPUT", "-s", subnet, "-j", CHAIN_IN], check=False).returncode == 0:
        pass
    for chain in (CHAIN_FWD, CHAIN_IN):
        _iptables(["-F", chain], check=False)
        _iptables(["-X", chain], check=False)


def ensure_ready() -> None:
    """Network + firewall in place - called before a sandbox is created or started."""
    ensure_network()
    ensure_firewall()
