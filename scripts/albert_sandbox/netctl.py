"""Network isolation of sandboxes.

Sandboxes live in dedicated Docker bridge networks whose inter-container
communication is disabled (``enable_icc=false``): a sandbox cannot reach any
other sandbox, not even its MCP Hub or file service.

* ``albert-sandbox`` (``ALBERT_NETWORK_SUBNET``): sandboxes of clients with
  internet access.
* ``albert-sandbox-offline`` (``ALBERT_OFFLINE_SUBNET``, created on first use):
  sandboxes of clients without internet access. Their containers also get a
  dead DNS server (``ALBERT_OFFLINE_DNS``), because Docker's embedded DNS
  would otherwise resolve external names for them from the host.
* ``albert-sandbox-c<id>`` (a subnet of ``ALBERT_CLIENT_SUBNET_POOL``): the
  own network of a client that got own internal targets. The firewall works
  on subnets, so a client's targets can only be opened for a network of its
  own (container addresses change on every start). Once a client has its own
  network it keeps it (and its subnet), also when its targets are removed
  again; the network is removed with the client.

Internal targets ("allow nets") are exceptions from the blocked internal
networks: the global list (admin UI *Einstellungen*, otherwise
``ALBERT_ALLOW_NETS``) applies to every sandbox, the list of a client
(column ``api_keys.allow_nets``) to its own sandboxes in addition.

Two iptables chains add egress rules for traffic coming from these networks:

* ``ALBERT-SANDBOX`` (hooked into Docker's ``DOCKER-USER`` chain, i.e. routed
  traffic): drops new connections to private/internal networks
  (``ALBERT_BLOCK_NETS``: RFC 1918, CGNAT, link-local incl. cloud metadata, ...)
  except the allowed ones. The internet stays reachable - except for offline
  networks, where everything but the allowed networks is dropped. A client
  network the rules do not know (yet) gets nothing at all.
* ``ALBERT-SANDBOX-IN`` (hooked into ``INPUT``): drops new connections from
  sandboxes to services on the host itself (ssh, nginx, ...). Replies to
  connections the host opened (nginx -> sandbox ports) are allowed.

Rules are applied idempotently by the manager before every create/start, when
an internal target changes, by the watcher every minute (self-healing) and at
boot by ``albert-sandbox-firewall.service``. A comment rule carrying a hash of
the configuration detects outdated rule sets.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import db, dockerctl
from .config import settings
from .errors import BadRequest, SandboxError

CHAIN_FWD = "ALBERT-SANDBOX"
CHAIN_IN = "ALBERT-SANDBOX-IN"

# Global internal targets set in the admin UI (kv); without it ALBERT_ALLOW_NETS applies.
ALLOW_NETS_KEY = "allow_nets"
MAX_ALLOW_NETS = 64


class NetworkError(SandboxError):
    code = "network"


# --------------------------------------------------------------------------
# where a sandbox belongs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    """Docker network a sandbox belongs in and whether it is cut off from the internet."""
    network: str
    subnet: str
    bridge: str
    offline: bool
    setting: str
    client_id: Optional[int] = None  # set: the client's own network


Target = Union[bool, Placement]


def shared(offline: bool = False) -> Placement:
    s = settings()
    if offline:
        return Placement(s.offline_network_name, s.offline_network_subnet, s.offline_network_bridge, True,
                         "ALBERT_OFFLINE_SUBNET")
    return Placement(s.network_name, s.network_subnet, s.network_bridge, False, "ALBERT_NETWORK_SUBNET")


def client_network_name(client_id: int) -> str:
    return f"{settings().network_name}-c{int(client_id)}"


def client_id_of_network(name: str) -> Optional[int]:
    match = re.fullmatch(re.escape(settings().network_name) + r"-c(\d+)", name or "")
    return int(match.group(1)) if match else None


def placement(client: sqlite3.Row) -> Placement:
    """Network of a client's sandboxes: its own one once it has a subnet, else the shared one."""
    offline = not bool(client["allow_internet"])
    subnet = _column(client, "net_subnet")
    if subnet:
        return Placement(client_network_name(client["id"]), subnet, f"br-albert-c{int(client['id'])}"[:15],
                         offline, "api_keys.net_subnet", int(client["id"]))
    return shared(offline)


def _column(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _as_placement(target: Target) -> Placement:
    return target if isinstance(target, Placement) else shared(bool(target))


# --------------------------------------------------------------------------
# internal targets
# --------------------------------------------------------------------------

def _reserved() -> List[ipaddress.IPv4Network]:
    s = settings()
    nets = []
    for value in (s.network_subnet, s.offline_network_subnet, s.client_subnet_pool):
        try:
            nets.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            pass
    return nets


def parse_nets(value: Any) -> List[str]:
    """Validate a list of internal targets (IPv4 addresses or networks).

    Accepts a list or a text with one entry per line (or separated by commas).
    Returns the normalised networks without duplicates, in the given order.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items: List[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise BadRequest("allowNets must be a list of IPv4 addresses or networks", code="invalid_net")
    entries: List[str] = []
    for item in items:
        if not isinstance(item, str):
            raise BadRequest("allowNets must be a list of IPv4 addresses or networks", code="invalid_net")
        entries += [part for part in re.split(r"[\s,;]+", item) if part]
    result: List[str] = []
    reserved = _reserved()
    for entry in entries:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise BadRequest(f"'{entry}' is not an IPv4 address or network (e.g. 10.0.5.20 or 10.0.5.0/24)",
                             code="invalid_net")
        if net.version != 4:
            raise BadRequest(f"'{entry}': only IPv4 targets are supported", code="invalid_net")
        if net.prefixlen == 0:
            raise BadRequest("0.0.0.0/0 would open every network; switch on internet access instead",
                             code="invalid_net")
        for other in reserved:
            if net.overlaps(other):
                raise BadRequest(f"{net} overlaps the sandbox networks ({other})", code="invalid_net")
        text = str(net)
        if text not in result:
            result.append(text)
    if len(result) > MAX_ALLOW_NETS:
        raise BadRequest(f"at most {MAX_ALLOW_NETS} internal targets", code="invalid_net")
    return result


def client_nets(client: Any) -> List[str]:
    """Own internal targets of a client (column allow_nets)."""
    raw = _column(client, "allow_nets")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def global_allow_nets(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Internal targets of all sandboxes: {"nets": [...], "source": "settings" | "config"}."""
    def read(c: sqlite3.Connection) -> Dict[str, Any]:
        stored = db.kv_get(c, ALLOW_NETS_KEY)
        if isinstance(stored, dict) and isinstance(stored.get("nets"), list):
            return {"nets": [str(n) for n in stored["nets"]], "source": "settings",
                    "updatedAt": stored.get("updatedAt")}
        return {"nets": list(settings().allow_nets), "source": "config"}
    if conn is not None:
        return read(conn)
    with db.session() as c:
        return read(c)


def set_global_allow_nets(conn: sqlite3.Connection, value: Any) -> List[str]:
    nets = parse_nets(value)
    db.kv_set(conn, ALLOW_NETS_KEY, {"nets": nets, "updatedAt": int(time.time())})
    return nets


def reset_global_allow_nets(conn: sqlite3.Connection) -> None:
    """Back to ALBERT_ALLOW_NETS from the configuration file."""
    conn.execute("DELETE FROM kv WHERE key=?", (ALLOW_NETS_KEY,))


def _docker_subnets() -> List[ipaddress.IPv4Network]:
    try:
        nets = []
        for item in dockerctl.network_list():
            for subnet in item["subnets"]:
                try:
                    nets.append(ipaddress.ip_network(subnet, strict=False))
                except ValueError:
                    pass
        return nets
    except dockerctl.DockerError:
        return []


def assign_subnet(conn: sqlite3.Connection, client_id: int) -> str:
    """Subnet of the client's own network; assigned once from ALBERT_CLIENT_SUBNET_POOL."""
    row = conn.execute("SELECT net_subnet FROM api_keys WHERE id=?", (client_id,)).fetchone()
    if row and row["net_subnet"]:
        return row["net_subnet"]
    s = settings()
    try:
        pool = ipaddress.ip_network(s.client_subnet_pool, strict=False)
    except ValueError:
        raise NetworkError(f"ALBERT_CLIENT_SUBNET_POOL '{s.client_subnet_pool}' is not a network")
    prefix = min(max(s.client_subnet_prefix, pool.prefixlen), 28)
    taken = {r["net_subnet"] for r in conn.execute("SELECT net_subnet FROM api_keys WHERE net_subnet IS NOT NULL")}
    in_use = _docker_subnets()
    for candidate in pool.subnets(new_prefix=prefix):
        text = str(candidate)
        if text in taken or any(candidate.overlaps(other) for other in in_use):
            continue
        try:
            conn.execute("UPDATE api_keys SET net_subnet=? WHERE id=?", (text, client_id))
        except sqlite3.IntegrityError:  # taken by a concurrent assignment
            taken.add(text)
            continue
        return text
    raise NetworkError(f"no free subnet left in ALBERT_CLIENT_SUBNET_POOL ({s.client_subnet_pool})")


# --------------------------------------------------------------------------
# docker network
# --------------------------------------------------------------------------

def network_name(target: Target = False) -> str:
    return _as_placement(target).network


def ensure_network(target: Target = False) -> Dict[str, Any]:
    """Create the sandbox network if missing and verify its isolation settings."""
    p = _as_placement(target)
    info = dockerctl.network_inspect(p.network)
    if info is None:
        labels = {dockerctl.LABEL_MANAGER: "1"}
        if p.client_id is not None:
            labels[dockerctl.LABEL_CLIENT] = str(p.client_id)
        dockerctl.network_create(p.network, p.subnet, p.bridge, labels)
        info = dockerctl.network_inspect(p.network)
        if info is None:
            raise NetworkError(f"could not create docker network {p.network}")
    options = info.get("Options") or {}
    if options.get("com.docker.network.bridge.enable_icc", "true") != "false":
        raise NetworkError(
            f"docker network {p.network} allows inter-container traffic; "
            "remove it (docker network rm) so the manager can recreate it isolated")
    actual = _subnet_of(info)
    if actual and actual != p.subnet:
        # The boot-time firewall uses the configured subnet, so both must agree.
        raise NetworkError(
            f"docker network {p.network} uses subnet {actual}, but {p.setting} is "
            f"{p.subnet}; set {p.setting}={actual} or recreate the network")
    return info


def _subnet_of(info: Dict[str, Any]) -> Optional[str]:
    for cfg in ((info.get("IPAM") or {}).get("Config") or []):
        if cfg.get("Subnet") and ":" not in cfg["Subnet"]:
            return cfg["Subnet"]
    return None


def network_subnet(info: Optional[Dict[str, Any]] = None, use_docker: bool = True, offline: bool = False) -> str:
    """Subnet of a shared sandbox network. ``use_docker=False`` (boot, before dockerd
    runs - asking docker would deadlock on socket activation) uses the configured value."""
    p = shared(offline)
    if use_docker:
        info = info or dockerctl.network_inspect(p.network) or {}
        actual = _subnet_of(info)
        if actual:
            return actual
    return p.subnet


def container_networks(info: Optional[Dict[str, Any]]) -> List[str]:
    return sorted(((info or {}).get("NetworkSettings") or {}).get("Networks", {}).keys())


def is_isolated(info: Optional[Dict[str, Any]], target: Target = False) -> bool:
    return container_networks(info) == [network_name(target)]


def dns_matches(info: Optional[Dict[str, Any]], target: Target) -> bool:
    """Offline containers must use the dead DNS server, online ones must not."""
    offline = _as_placement(target).offline
    dns = ((info or {}).get("HostConfig") or {}).get("Dns") or []
    dead = settings().offline_dns
    return dns == [dead] if offline else dead not in dns


def run_args(target: Target) -> List[str]:
    """``docker run`` arguments that put a container into its sandbox network."""
    p = _as_placement(target)
    args = ["--network", p.network]
    if p.offline:
        args += ["--dns", settings().offline_dns]
    return args


def isolate(name: str, info: Optional[Dict[str, Any]] = None, target: Target = False) -> bool:
    """Move a container into its sandbox network (works while it runs).

    Published ports stay as they are; returns True if something changed.
    """
    info = info or dockerctl.inspect_one(name)
    if info is None:
        return False
    p = _as_placement(target)
    nets = container_networks(info)
    if nets == [p.network]:
        return False
    if p.network not in nets:
        if p.offline or p.client_id is not None:
            ensure_network(p)
        dockerctl.network_connect(p.network, name)
    for other in nets:
        if other != p.network:
            dockerctl.network_disconnect(other, name)
    return True


def stale_client_networks(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Own networks of clients that no longer exist (or lost their subnet)."""
    subnets = {r["id"]: r["net_subnet"] for r in conn.execute("SELECT id, net_subnet FROM api_keys")}
    stale = []
    for item in dockerctl.network_list():
        client_id = client_id_of_network(item["name"])
        if client_id is None or (item.get("labels") or {}).get(dockerctl.LABEL_MANAGER) != "1":
            continue
        if subnets.get(client_id) and subnets[client_id] in item["subnets"]:
            continue
        stale.append(item)
    return stale


# --------------------------------------------------------------------------
# firewall
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientRule:
    subnet: str
    offline: bool
    nets: Tuple[str, ...]


def _iptables(args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["iptables", "-w", "10", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise NetworkError("iptables not found - cannot isolate sandboxes") from exc
    if check and proc.returncode != 0:
        raise NetworkError(f"iptables {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def policy() -> Tuple[List[str], List[ClientRule]]:
    """Global internal targets and the rules of every client network, from the database.

    If the database cannot be read, nothing is allowed (the rules fail closed;
    the watcher applies the real ones a minute later)."""
    try:
        with db.session() as conn:
            global_nets = global_allow_nets(conn)["nets"]
            rules = [ClientRule(r["net_subnet"], not bool(r["allow_internet"]), tuple(client_nets(r)))
                     for r in conn.execute("SELECT id, net_subnet, allow_nets, allow_internet FROM api_keys "
                                           "WHERE net_subnet IS NOT NULL ORDER BY net_subnet")]
        return global_nets, rules
    except sqlite3.Error:
        return [], []


def desired_rules(subnet: str, offline_subnet: str, global_nets: Optional[Iterable[str]] = None,
                  client_rules: Iterable[ClientRule] = ()) -> Tuple[List[List[str]], List[List[str]], str]:
    """Rule specs for both chains plus a fingerprint of the configuration.

    ``global_nets`` defaults to ALBERT_ALLOW_NETS (the caller passes the
    database state, see policy())."""
    s = settings()
    allow = list(s.allow_nets) if global_nets is None else list(global_nets)
    fwd: List[List[str]] = [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"]]
    for net in allow:
        fwd.append(["-s", subnet, "-d", net, "-j", "RETURN"])
    for net in s.block_nets:
        fwd.append(["-s", subnet, "-d", net, "-j", "DROP"])
    # Offline sandboxes: only the explicitly allowed internal networks.
    for net in allow:
        fwd.append(["-s", offline_subnet, "-d", net, "-j", "RETURN"])
    fwd.append(["-s", offline_subnet, "-j", "DROP"])
    # Own networks of clients: global plus own targets.
    for rule in client_rules:
        for net in allow + [n for n in rule.nets if n not in allow]:
            fwd.append(["-s", rule.subnet, "-d", net, "-j", "RETURN"])
        if rule.offline:
            fwd.append(["-s", rule.subnet, "-j", "DROP"])
        else:
            for net in s.block_nets:
                fwd.append(["-s", rule.subnet, "-d", net, "-j", "DROP"])
            fwd.append(["-s", rule.subnet, "-j", "RETURN"])
    # A client network without rules (unknown to the database) gets nothing.
    fwd.append(["-s", s.client_subnet_pool, "-j", "DROP"])
    inp: List[List[str]] = [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"]]
    if s.block_host_access:
        inp.append(["-j", "DROP"])
    fingerprint = hashlib.sha256(repr((subnet, offline_subnet, fwd, inp)).encode()).hexdigest()[:16]
    return fwd, inp, fingerprint


def _marker(fingerprint: str) -> List[str]:
    """No-op rule (source 255.255.255.255 never occurs) carrying the config fingerprint."""
    return ["-s", "255.255.255.255/32", "-m", "comment", "--comment", f"albert-sandbox {fingerprint}",
            "-j", "RETURN"]


def _subnets(use_docker: bool) -> Tuple[str, str]:
    return network_subnet(use_docker=use_docker), network_subnet(use_docker=use_docker, offline=True)


def _input_sources(subnet: str, offline_subnet: str, client_rules: Iterable[ClientRule] = ()) -> List[str]:
    """Sources whose connections to the host go through CHAIN_IN: the shared networks,
    the client pool and client networks outside the pool (pool changed later)."""
    pool = settings().client_subnet_pool
    sources = [subnet, offline_subnet, pool]
    try:
        pool_net: Optional[ipaddress.IPv4Network] = ipaddress.ip_network(pool, strict=False)
    except ValueError:
        pool_net = None
    for rule in client_rules:
        try:
            inside = pool_net is not None and ipaddress.ip_network(rule.subnet, strict=False).subnet_of(pool_net)
        except (TypeError, ValueError):
            inside = False
        if not inside and rule.subnet not in sources:
            sources.append(rule.subnet)
    return sources


def _restore(chains: Sequence[Tuple[str, List[List[str]]]], fingerprint: str) -> None:
    """Replace the chains in one step (iptables-restore): flushing them first and
    adding the rules one by one would leave them empty - i.e. open - for a moment."""
    lines = ["*filter"]
    lines += [f":{chain} - [0:0]" for chain, _ in chains]
    marker = " ".join(_marker(fingerprint)).replace(f"albert-sandbox {fingerprint}",
                                                     f'"albert-sandbox {fingerprint}"')
    for chain, rules in chains:
        lines.append(f"-A {chain} {marker}")
        lines += [" ".join(["-A", chain, *rule]) for rule in rules]
    lines.append("COMMIT")
    try:
        proc = subprocess.run(["iptables-restore", "-w", "10", "--noflush"], input="\n".join(lines) + "\n",
                              capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise NetworkError("iptables-restore not found - cannot isolate sandboxes") from exc
    if proc.returncode != 0:
        raise NetworkError(f"iptables-restore failed: {proc.stderr.strip()}")


def firewall_status(use_docker: bool = True) -> Dict[str, Any]:
    s = settings()
    if not s.firewall:
        return {"enabled": False, "ok": True}
    subnet, offline_subnet = _subnets(use_docker)
    global_nets, rules = policy()
    _, _, fingerprint = desired_rules(subnet, offline_subnet, global_nets, rules)
    checks = {
        "forwardChain": _iptables(["-C", CHAIN_FWD, *_marker(fingerprint)], check=False).returncode == 0,
        "inputChain": _iptables(["-C", CHAIN_IN, *_marker(fingerprint)], check=False).returncode == 0,
        "dockerUserJump": _iptables(["-C", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode == 0,
        "inputJump": _iptables(["-C", "INPUT", "-s", subnet, "-j", CHAIN_IN], check=False).returncode == 0,
        "inputJumpOffline": _iptables(["-C", "INPUT", "-s", offline_subnet, "-j", CHAIN_IN],
                                      check=False).returncode == 0,
        "inputJumpClients": all(
            _iptables(["-C", "INPUT", "-s", net, "-j", CHAIN_IN], check=False).returncode == 0
            for net in _input_sources(subnet, offline_subnet, rules)[2:]),
    }
    return {"enabled": True, "ok": all(checks.values()), "subnet": subnet, "offlineSubnet": offline_subnet,
            "clientSubnetPool": s.client_subnet_pool, "clientNetworks": len(rules),
            "fingerprint": fingerprint, **checks}


def apply_firewall(use_docker: bool = True) -> Dict[str, Any]:
    """(Re)build both chains and make sure they are hooked in. Idempotent.

    ``use_docker=False`` is used at boot (before dockerd starts) and never talks to docker.
    """
    s = settings()
    if not s.firewall:
        return {"enabled": False, "applied": False}
    subnet, offline_subnet = _subnets(use_docker)
    global_nets, rules = policy()
    fwd, inp, fingerprint = desired_rules(subnet, offline_subnet, global_nets, rules)
    _restore(((CHAIN_FWD, fwd), (CHAIN_IN, inp)), fingerprint)
    # DOCKER-USER exists once dockerd ran; create it otherwise (dockerd keeps it).
    if _iptables(["-n", "-L", "DOCKER-USER"], check=False).returncode != 0:
        _iptables(["-N", "DOCKER-USER"])
    if _iptables(["-C", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode != 0:
        _iptables(["-I", "DOCKER-USER", "1", "-j", CHAIN_FWD])
    for net in _input_sources(subnet, offline_subnet, rules):
        if _iptables(["-C", "INPUT", "-s", net, "-j", CHAIN_IN], check=False).returncode != 0:
            _iptables(["-I", "INPUT", "1", "-s", net, "-j", CHAIN_IN])
    return {"enabled": True, "applied": True, "subnet": subnet, "offlineSubnet": offline_subnet,
            "clientNetworks": len(rules), "fingerprint": fingerprint}


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
    while _iptables(["-D", "DOCKER-USER", "-j", CHAIN_FWD], check=False).returncode == 0:
        pass
    for net in _input_sources(*_subnets(use_docker=True), policy()[1]):
        while _iptables(["-D", "INPUT", "-s", net, "-j", CHAIN_IN], check=False).returncode == 0:
            pass
    for chain in (CHAIN_FWD, CHAIN_IN):
        _iptables(["-F", chain], check=False)
        _iptables(["-X", chain], check=False)


def ensure_ready(target: Target = False) -> None:
    """Network + firewall in place - called before a sandbox is created or started."""
    p = _as_placement(target)
    ensure_network()
    if p.client_id is not None or p.offline:
        ensure_network(p)
    ensure_firewall()
