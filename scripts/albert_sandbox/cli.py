"""Command line interface (``albert-ai-sandbox-manager``).

Keeps the commands and JSON output of the former Bash manager and adds
operator commands for clients, admin users, cleanup and disks.

Without --api-key/--api-key-hash, sandbox commands run in operator mode and
see all sandboxes (``create`` always needs a key: it decides the owner).
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from typing import Any, Dict, List, Optional

from . import admins, clients, db, dockerctl, netctl, profiles, reconcile, sandboxes, watcher
from .config import settings
from .errors import NotFound, SandboxError

GREEN, YELLOW, RED, BLUE, NC = "\033[0;32m", "\033[1;33m", "\033[0;31m", "\033[0;34m", "\033[0m"

SANDBOX_COMMANDS = ("create", "remove", "delete", "start", "stop", "restart", "status", "list", "resources")


class CliError(Exception):
    def __init__(self, message: str, code: str = "error", exit_code: int = 1) -> None:
        super().__init__(message)
        self.message, self.code, self.exit_code = message, code, exit_code


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    s = argparse.SUPPRESS
    p.add_argument("--json", action="store_true", default=s, help="JSON output")
    p.add_argument("--quiet", action="store_true", default=s, help="suppress normal output")
    p.add_argument("--api-key", default=s, metavar="KEY", help="act as the client owning this API key")
    p.add_argument("--api-key-hash", default=s, metavar="HASH", help="same, with the SHA-256 hash of the key")
    p.add_argument("--non-interactive", action="store_true", default=s, help="never prompt")
    p.add_argument("--remove-volumes", action="store_true", default=s, help="(compat, data is always removed)")
    p.add_argument("--debug", action="store_true", default=s, help="show tracebacks")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog="albert-ai-sandbox-manager", parents=[common],
                                     description="ALBERT | AI Sandbox manager")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str, **kw: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text, **kw)

    p = add("create", "create a sandbox (needs --api-key)")
    p.add_argument("name", nargs="?", help="optional name hint")
    p.add_argument("--persistent", action="store_true", help="exclude from automatic max-age removal")
    p.add_argument("--cpus", type=float)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--disk-gb", type=int)

    for name, text in (("remove", "remove a sandbox and all its data"), ("delete", "alias for remove"),
                       ("start", "start a sandbox"), ("stop", "stop a sandbox"), ("restart", "restart a sandbox")):
        add(name, text).add_argument("name")
    add("status", "status of one or all sandboxes").add_argument("name", nargs="?")
    add("list", "list sandboxes")

    p = add("resources", "change limits of a sandbox (disk can only grow)")
    p.add_argument("name")
    p.add_argument("--cpus", type=float)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--disk-gb", type=int)

    p = add("persistent", "set or clear the persistent flag")
    p.add_argument("name")
    p.add_argument("value", choices=["on", "off"])

    p = add("recreate", "rebuild sandbox containers with the current image, hardening and network "
                        "(data, ports and credentials are kept)")
    p.add_argument("name", nargs="?")
    p.add_argument("--outdated", action="store_true", help="rebuild every outdated sandbox")
    p.add_argument("--force", action="store_true", help="also rebuild sandboxes stuck in 'rebuilding'")

    p = add("firewall", "network isolation of the sandboxes (apply is idempotent)")
    p.add_argument("action", nargs="?", choices=["apply", "status", "remove"], default="apply")
    p.add_argument("--boot", action="store_true",
                   help="apply the rules without contacting docker (boot, before dockerd starts)")

    add("build", "rebuild the sandbox Docker image")
    p = add("reconcile", "remove leftovers and repair routes/mounts")
    p.add_argument("--dry-run", action="store_true", help="only show what would be done")
    add("migrate", "upgrade the database and import the legacy JSON registry")
    add("mount-disks", "mount all sandbox disk images (boot)")
    add("watch", "run the inactivity watcher once")
    add("usage-refresh", "measure disk usage of all sandboxes")
    add("selfcheck", "show configuration and health")

    p = add("client", "manage clients (API keys)")
    csub = p.add_subparsers(dest="client_command")
    cp = csub.add_parser("create", parents=[common], help="create a client and print its API key")
    cp.add_argument("--label", default="")
    _client_options(cp)
    csub.add_parser("list", parents=[common], help="list clients")
    cp = csub.add_parser("show", parents=[common], help="show a client")
    cp.add_argument("id", type=int)
    cp = csub.add_parser("set", parents=[common], help="change label, defaults or quotas (0 = none allowed, "
                                                       "'none' = unlimited / global default)")
    cp.add_argument("id", type=int)
    cp.add_argument("--label")
    _client_options(cp)
    cp = csub.add_parser("rotate", parents=[common], help="issue a new API key (old one stops working)")
    cp.add_argument("id", type=int)
    cp = csub.add_parser("delete", parents=[common], help="delete a client and all its sandboxes")
    cp.add_argument("id", type=int, nargs="?")
    cp.add_argument("--key", help="identify the client by plaintext API key instead of id")

    p = add("profile", "manage sandbox profiles (extra software on top of the standard image)")
    psub = p.add_subparsers(dest="profile_command")
    psub.add_parser("list", parents=[common], help="list profiles")
    pp = psub.add_parser("show", parents=[common], help="show a profile incl. generated Dockerfile")
    pp.add_argument("profile", help="id or name")
    pp = psub.add_parser("create", parents=[common], help="create a profile")
    pp.add_argument("--name", required=True)
    _profile_options(pp)
    pp = psub.add_parser("set", parents=[common], help="change a profile (rebuild it afterwards)")
    pp.add_argument("profile", help="id or name")
    pp.add_argument("--name")
    _profile_options(pp)
    pp = psub.add_parser("delete", parents=[common], help="delete a profile no client uses")
    pp.add_argument("profile", help="id or name")
    pp = psub.add_parser("build", parents=[common], help="build the profile image (shows the build output)")
    pp.add_argument("profile", help="id or name")
    pp.add_argument("--background", action="store_true", help="start the build job and return at once")
    psub.add_parser("rebuild-outdated", parents=[common],
                    help="rebuild all profiles whose image is outdated (install.sh, after a base image update)")
    pp = psub.add_parser("log", parents=[common], help="show the output of the last (or a given) build")
    pp.add_argument("profile", help="id or name")
    pp.add_argument("--seq", type=int)
    pp = psub.add_parser("build-job", parents=[common], help=argparse.SUPPRESS)
    pp.add_argument("profile_id", type=int)
    pp.add_argument("seq", type=int)

    p = add("admin", "manage admin users of the web UI")
    asub = p.add_subparsers(dest="admin_command")
    ap = asub.add_parser("add", parents=[common], help="add an admin user")
    ap.add_argument("username")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    ap.add_argument("--generate", action="store_true", help="generate and print a random password")
    asub.add_parser("list", parents=[common], help="list admin users")
    ap = asub.add_parser("passwd", parents=[common], help="set a new password")
    ap.add_argument("username")
    ap.add_argument("--password-stdin", action="store_true")
    ap.add_argument("--generate", action="store_true")
    ap = asub.add_parser("remove", parents=[common], help="delete an admin user")
    ap.add_argument("username")
    add("help", "show this help")
    return parser


def _client_options(p: argparse.ArgumentParser) -> None:
    for flag in ("--default-cpus", "--default-memory-mb", "--default-disk-gb",
                 "--quota-cpus", "--quota-memory-mb", "--quota-disk-gb", "--quota-sandboxes"):
        p.add_argument(flag)
    p.add_argument("--quota-running", help="sandboxes running at the same time")
    p.add_argument("--allow-persistent", choices=("yes", "no"),
                   help="may the client create persistent sandboxes (new clients: no)")
    p.add_argument("--internet", choices=("yes", "no"),
                   help="internet access of the client's sandboxes (default: yes)")
    p.add_argument("--profile", help="sandbox profile (id or name; 'none' = standard image)")


def _profile_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--description")
    p.add_argument("--apt", help="OS packages, space separated (replaces the list)")
    p.add_argument("--pip", help="Python packages, space separated (replaces the list)")
    p.add_argument("--npm", help="global Node packages, space separated (replaces the list)")
    p.add_argument("--setup-file", help="shell script run as root at the end of the build ('-' = none)")


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

class Out:
    def __init__(self, args: argparse.Namespace) -> None:
        self.json = bool(getattr(args, "json", False))
        self.quiet = bool(getattr(args, "quiet", False))

    def emit(self, payload: Any, text: Optional[str] = None) -> None:
        if self.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif not self.quiet and text is not None:
            print(text)


def _fmt_bytes(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _limits(item: Dict[str, Any]) -> str:
    r = item.get("resources") or {}
    cpus = f"{r['cpus']:g}" if r.get("cpus") else "-"
    mem = f"{r['memoryMb']}M" if r.get("memoryMb") else "-"
    disk = f"{r['diskGb']}G" if r.get("diskGb") else "-"
    return f"{cpus} CPU / {mem} / {disk}"


def _sandbox_text(item: Dict[str, Any]) -> str:
    lines = [
        f"{BLUE}Sandbox:{NC} {item['name']}",
        f"{BLUE}Status:{NC} {item['status']}",
        f"{BLUE}Created:{NC} {item.get('createdAt')}",
        f"{BLUE}Persistent:{NC} {item.get('persistent')}",
        f"{BLUE}Limits:{NC} {_limits(item)}",
        f"{BLUE}Disk used:{NC} {_fmt_bytes((item.get('usage') or {}).get('diskUsedBytes'))}",
        f"{BLUE}Desktop:{NC} {item['urls']['desktop']}",
        f"{BLUE}MCP URL:{NC} {item['urls']['mcphub']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _resolve_client(args: argparse.Namespace, required: bool) -> Optional[Any]:
    key = getattr(args, "api_key", None) or getattr(args, "api_key_hash", None)
    if not key:
        if required:
            raise CliError("This operation requires an API key. Use --api-key <PLAINTEXT> or --api-key-hash <HASH>.",
                           "API key required", 2)
        return None
    with db.session() as conn:
        row = clients.find_by_key(conn, key)
    if not row:
        raise CliError("Provided API key not registered.", "Unknown API key", 2)
    return row


def cmd_sandbox(args: argparse.Namespace, out: Out) -> None:
    command = args.command
    client = _resolve_client(args, required=(command == "create"))
    client_id = client["id"] if client else None
    actor = f"cli:{client_id}" if client else "cli:operator"

    if command == "create":
        resources = {"cpus": args.cpus, "memoryMb": args.memory_mb, "diskGb": args.disk_gb}
        result = sandboxes.create(client_id, requested_name=args.name, persistent=args.persistent,
                                  resources=resources, actor=actor)
        result["ownerHash"] = client["key_hash"]
        out.emit(result, "\n".join([
            f"{GREEN}Sandbox created: {result['name']}{NC}",
            f"{GREEN}DESKTOP: {result['urls']['desktop']}{NC}",
            f"{GREEN}MCP URL: {result['urls']['mcphub']}{NC}",
            f"{GREEN}File Service Upload: {result['urls']['filesUpload']}{NC}",
            f"Limits: {_limits(result)}",
            f"{YELLOW}VNC Password: {result['vncPassword']}{NC}",
            f"{YELLOW}File Service Token: {result['fileServiceToken']}{NC}",
            f"{YELLOW}MCP Hub Bearer token: albert{NC}",
        ]))
    elif command in ("remove", "delete"):
        sandboxes.remove(args.name, client_id, actor=actor)
        out.emit({"result": "removed", "name": args.name}, f"{GREEN}Sandbox '{args.name}' removed{NC}")
    elif command == "start":
        item = sandboxes.start(args.name, client_id, actor=actor)
        out.emit({"result": "started", "name": args.name, "url": item["urls"]["desktop"],
                  "host": settings().public_url, "container": item},
                 f"{GREEN}Sandbox '{args.name}' started: {item['urls']['desktop']}{NC}")
    elif command == "stop":
        sandboxes.stop(args.name, client_id, actor=actor)
        out.emit({"result": "stopped", "name": args.name}, f"{GREEN}Sandbox '{args.name}' stopped{NC}")
    elif command == "restart":
        sandboxes.restart(args.name, client_id, actor=actor)
        out.emit({"result": "restarted", "name": args.name}, f"{GREEN}Sandbox '{args.name}' restarted{NC}")
    elif command == "status" and args.name:
        item = sandboxes.describe(args.name, client_id)
        out.emit(item, _sandbox_text(item))
    elif command in ("status", "list"):
        items = sandboxes.list_sandboxes(client_id)
        if out.json:
            out.emit(items)
            return
        if out.quiet:
            for item in items:
                print(item["name"])
            return
        if not items:
            print(f"{YELLOW}(no sandboxes){NC}")
            return
        print(f"{'NAME':44} {'STATUS':9} {'CLIENT':16} {'LIMITS':26} DISK USED")
        for item in items:
            print(f"{item['name']:44} {item['status']:9} {str(item.get('clientLabel', client_id))[:16]:16} "
                  f"{_limits(item):26} {_fmt_bytes((item.get('usage') or {}).get('diskUsedBytes'))}")
    elif command == "resources":
        item = sandboxes.update_resources(args.name, cpus=args.cpus, memory_mb=args.memory_mb,
                                          disk_gb=args.disk_gb, client_id=client_id, actor=actor)
        out.emit(item, f"{GREEN}Limits of '{args.name}': {_limits(item)}{NC}")


def cmd_persistent(args: argparse.Namespace, out: Out) -> None:
    client = _resolve_client(args, required=False)
    result = sandboxes.set_persistent(args.name, args.value == "on", client["id"] if client else None,
                                      actor="cli")
    out.emit(result, f"{GREEN}{args.name}: persistent={result['persistent']}{NC}")


def _parse_optional(value: Optional[str]) -> Any:
    if value is None:
        return None
    if value.strip().lower() in ("none", "unlimited", "-", ""):
        return ""
    return value


def _client_fields_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for attr in ("default_cpus", "default_memory_mb", "default_disk_gb",
                 "quota_cpus", "quota_memory_mb", "quota_disk_gb", "quota_sandboxes", "quota_running"):
        value = _parse_optional(getattr(args, attr, None))
        if value is not None:
            fields[attr] = value
    if getattr(args, "allow_persistent", None):
        fields["allow_persistent"] = args.allow_persistent == "yes"
    if getattr(args, "internet", None):
        fields["allow_internet"] = args.internet == "yes"
    ref = getattr(args, "profile", None)
    if ref is not None:
        if ref.strip().lower() in ("none", "standard", "-", ""):
            fields["profile_id"] = None
        else:
            with db.session() as conn:
                fields["profile_id"] = profiles.find(conn, ref)["id"]
    return fields


def _client_summary(conn: Any, row: Any, running: set) -> Dict[str, Any]:
    return {
        "id": row["id"], "label": row["label"] or "", "keyHashPrefix": row["key_hash"][:12],
        "createdAt": sandboxes.iso(row["created_at"]),
        "defaults": {"cpus": row["default_cpus"], "memoryMb": row["default_memory_mb"],
                     "diskGb": row["default_disk_gb"]},
        "quota": clients.quotas(row), "usage": clients.usage(conn, row["id"], running),
        "allowPersistent": clients.persistent_allowed(row),
        "allowInternet": clients.internet_allowed(row),
        "profile": profiles.summary(profiles.get(conn, row["profile_id"])) if row["profile_id"] else None,
    }


def cmd_client(args: argparse.Namespace, out: Out) -> None:
    sub = args.client_command
    if sub == "create":
        with db.session() as conn:
            row, raw = clients.create(conn, args.label, **_client_fields_from_args(args))
            db.audit(conn, "cli", "client.create", args.label, {"clientId": row["id"]})
        out.emit({"id": row["id"], "label": row["label"], "key": raw},
                 f"API Key created:\n  id:    {row['id']}\n  label: {row['label']}\n  key:   {raw}\n"
                 "Store this key now; it will not be shown again.")
    elif sub == "list":
        running = dockerctl.running_names() if dockerctl.available() else set()
        with db.session() as conn:
            items = [_client_summary(conn, r, running) for r in clients.list_all(conn)]
        if out.json:
            out.emit(items)
            return
        if not items:
            print("No API keys.")
        for c in items:
            q, u = c["quota"], c["usage"]
            print(f"id={c['id']} label={c['label']} created={c['createdAt']} key_hash_prefix={c['keyHashPrefix']} "
                  f"sandboxes={u['sandboxes']}/{q['sandboxes'] if q['sandboxes'] is not None else '∞'} "
                  f"running={u['running']}/{q['running'] if q['running'] is not None else '∞'} "
                  f"persistent={'yes' if c['allowPersistent'] else 'no'} "
                  f"internet={'yes' if c['allowInternet'] else 'no'} "
                  f"profile={(c['profile'] or {}).get('name', 'standard')} "
                  f"cpus={u['cpus']:g}/{q['cpus'] if q['cpus'] is not None else '∞'} "
                  f"memMb={u['memoryMb']}/{q['memoryMb'] if q['memoryMb'] is not None else '∞'} "
                  f"diskGb={u['diskGb']}/{q['diskGb'] if q['diskGb'] is not None else '∞'}")
    elif sub == "show":
        running = dockerctl.running_names()
        with db.session() as conn:
            item = _client_summary(conn, clients.get(conn, args.id), running)
        item["sandboxes"] = sandboxes.list_sandboxes(args.id)
        out.emit(item, json.dumps(item, indent=2, ensure_ascii=False))
    elif sub == "set":
        fields = _client_fields_from_args(args)
        if args.label is not None:
            fields["label"] = args.label
        with db.session() as conn:
            before = clients.get(conn, args.id)
            row = clients.update(conn, args.id, fields)
            db.audit(conn, "cli", "client.update", row["label"], {"clientId": args.id, **fields})
        if bool(before["allow_internet"]) != bool(row["allow_internet"]):
            if not out.json and not out.quiet:
                print(f"{YELLOW}Internet access changed - moving/rebuilding the client's sandboxes ...{NC}",
                      flush=True)
            sandboxes.apply_network_policy(args.id, actor="cli")
        with db.session() as conn:
            item = _client_summary(conn, clients.get(conn, args.id), dockerctl.running_names())
        out.emit(item, json.dumps(item, indent=2, ensure_ascii=False))
    elif sub == "rotate":
        with db.session() as conn:
            raw = clients.rotate_key(conn, args.id)
            db.audit(conn, "cli", "client.rotate_key", str(args.id))
        out.emit({"id": args.id, "key": raw}, f"New API key for client {args.id}:\n  key:   {raw}\n"
                                              "The previous key no longer works.")
    elif sub == "delete":
        client_id = args.id
        if args.key:
            with db.session() as conn:
                row = clients.find_by_key(conn, args.key)
            if not row:
                raise CliError("API key not found", "not_found", 2)
            client_id = row["id"]
        if client_id is None:
            raise CliError("client id or --key required", "usage", 2)
        result = sandboxes.delete_client(client_id, actor="cli")
        out.emit(result, f"Client {client_id} deleted, removed sandboxes: {', '.join(result['removedSandboxes']) or '-'}\n"
                         "API key revoked.")
    else:
        raise CliError("usage: client {create,list,show,set,rotate,delete}", "usage", 2)


def _profile_fields_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for attr, column in (("name", "name"), ("description", "description"), ("apt", "apt_packages"),
                         ("pip", "pip_packages"), ("npm", "npm_packages")):
        value = getattr(args, attr, None)
        if value is not None:
            fields[column] = value
    setup = getattr(args, "setup_file", None)
    if setup is not None:
        if setup == "-":
            fields["setup_script"] = ""
        else:
            try:
                with open(setup, encoding="utf-8") as fh:
                    fields["setup_script"] = fh.read()
            except OSError as exc:
                raise CliError(f"cannot read {setup}: {exc}", "usage", 2)
    return fields


def _profile_text(item: Dict[str, Any]) -> str:
    pk = item["packages"]
    lines = [
        f"{BLUE}Profile:{NC} {item['name']} (id {item['id']})",
        f"{BLUE}Status:{NC} {item['status']}" + (f" ({', '.join(item['outdatedReasons'])})"
                                                if item["outdatedReasons"] else ""),
        f"{BLUE}Image:{NC} {item['image'] or '-'}  {_fmt_bytes(item['imageSizeBytes'])}  built {item['builtAt'] or '-'}",
        f"{BLUE}apt:{NC} {' '.join(pk['apt']) or '-'}",
        f"{BLUE}pip:{NC} {' '.join(pk['pip']) or '-'}",
        f"{BLUE}npm:{NC} {' '.join(pk['npm']) or '-'}",
        f"{BLUE}Clients:{NC} {', '.join(c['label'] or str(c['id']) for c in item.get('clients', [])) or '-'}",
    ]
    if item["build"]["state"] == "failed" and item["build"]["error"]:
        lines.append(f"{RED}Last build failed:{NC} {item['build']['error'].splitlines()[0]}")
    return "\n".join(lines)


def cmd_profile(args: argparse.Namespace, out: Out) -> None:
    sub = args.profile_command
    if sub == "build-job":
        ok = profiles.run_build(args.profile_id, args.seq)
        if not ok:
            raise CliError("profile build failed", "build_failed", 1)
        return
    if sub == "rebuild-outdated":
        echo = None if (out.json or out.quiet) else (lambda line: print(line, flush=True))
        results = profiles.rebuild_outdated("cli", echo=echo)
        out.emit(results, "\n".join(f"{k}: {v}" for k, v in results.items()) or "All profile images are up to date.")
        if any(v != "ok" for v in results.values()):
            raise CliError("some profiles could not be rebuilt", "build_failed", 1)
        return
    base_id = profiles.base_image_id()
    if sub == "list":
        with db.session() as conn:
            items = [profiles.serialize(conn, r, base_id) for r in profiles.list_all(conn)]
        if out.json:
            out.emit(items)
            return
        if not items:
            print("No profiles.")
        for it in items:
            pk = it["packages"]
            print(f"id={it['id']} name={it['name']} status={it['status']} image={it['image'] or '-'} "
                  f"apt={len(pk['apt'])} pip={len(pk['pip'])} npm={len(pk['npm'])} "
                  f"clients={len(it.get('clients', []))}")
        return
    if sub == "create":
        fields = _profile_fields_from_args(args)
        name = fields.pop("name")
        with db.session() as conn:
            row = profiles.create(conn, name, **fields)
            db.audit(conn, "cli", "profile.create", row["name"], {"profileId": row["id"]})
            item = profiles.serialize(conn, row, base_id)
        out.emit(item, _profile_text(item) + f"\nBuild it with: albert-ai-sandbox-manager profile build {row['id']}")
        return
    with db.session() as conn:
        row = profiles.find(conn, args.profile)
    if sub == "show":
        with db.session() as conn:
            item = profiles.serialize(conn, row, base_id)
        item["dockerfile"] = profiles.dockerfile(row)
        out.emit(item, _profile_text(item) + "\n\n" + item["dockerfile"])
    elif sub == "set":
        fields = _profile_fields_from_args(args)
        with db.session() as conn:
            row = profiles.update(conn, row["id"], fields)
            db.audit(conn, "cli", "profile.update", row["name"], {"profileId": row["id"], "fields": sorted(fields)})
            item = profiles.serialize(conn, row, base_id)
        out.emit(item, _profile_text(item))
    elif sub == "delete":
        with db.session() as conn:
            profiles.delete(conn, row["id"])
            db.audit(conn, "cli", "profile.delete", row["name"], {"profileId": row["id"]})
        out.emit({"deleted": row["id"]}, f"Profile '{row['name']}' deleted (its images are removed by reconcile).")
    elif sub == "build":
        queued = profiles.request_build(row["id"], "cli", spawn=args.background)
        if args.background:
            out.emit({"profile": row["id"], "seq": queued["build_seq"], "state": "queued"},
                     f"Build {queued['build_seq']} of '{row['name']}' started; "
                     f"follow it with: albert-ai-sandbox-manager profile log {row['id']}")
            return
        echo = None if (out.json or out.quiet) else (lambda line: print(line, flush=True))
        ok = profiles.run_build(row["id"], queued["build_seq"], echo=echo)
        with db.session() as conn:
            item = profiles.serialize(conn, profiles.get(conn, row["id"]), profiles.base_image_id())
        out.emit(item, f"{GREEN if ok else RED}Build {'finished' if ok else 'failed'}: {item['image'] or '-'}{NC}")
        if not ok:
            raise CliError("profile build failed", "build_failed", 1)
    elif sub == "log":
        data = profiles.read_log(row["id"], seq=args.seq)
        out.emit(data, data["text"] or "(no build output)")
    else:
        raise CliError("usage: profile {list,show,create,set,delete,build,rebuild-outdated,log}", "usage", 2)


def _read_password(args: argparse.Namespace, out: Out) -> str:
    if getattr(args, "generate", False):
        return secrets.token_urlsafe(15)
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
        return secrets.token_urlsafe(15)
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise CliError("passwords do not match", "usage", 2)
    return first


def cmd_admin(args: argparse.Namespace, out: Out) -> None:
    sub = args.admin_command
    with db.session() as conn:
        if sub == "add":
            password = _read_password(args, out)
            row = admins.create(conn, args.username, password)
            db.audit(conn, "cli", "admin.create", row["username"])
            shown = password if (args.generate or not sys.stdin.isatty() or args.non_interactive) \
                and not args.password_stdin else None
            out.emit({"username": row["username"], "password": shown},
                     f"Admin user '{row['username']}' created." + (f"\n  password: {shown}" if shown else ""))
        elif sub == "list":
            rows = admins.list_all(conn)
            out.emit([{"id": r["id"], "username": r["username"], "createdAt": sandboxes.iso(r["created_at"]),
                       "lastLoginAt": sandboxes.iso(r["last_login_at"])} for r in rows],
                     "\n".join(f"{r['username']}  (created {sandboxes.iso(r['created_at'])}, "
                               f"last login {sandboxes.iso(r['last_login_at']) or '-'})" for r in rows)
                     or "No admin users.")
        elif sub == "passwd":
            row = admins.find_by_name(conn, args.username)
            if not row:
                raise CliError(f"admin user '{args.username}' not found", "not_found", 3)
            password = _read_password(args, out)
            admins.set_password(conn, row["id"], password)
            db.audit(conn, "cli", "admin.password", row["username"])
            shown = password if args.generate else None
            out.emit({"username": row["username"], "password": shown},
                     "Password changed." + (f"\n  password: {shown}" if shown else ""))
        elif sub == "remove":
            row = admins.find_by_name(conn, args.username)
            if not row:
                raise CliError(f"admin user '{args.username}' not found", "not_found", 3)
            admins.delete(conn, row["id"])
            db.audit(conn, "cli", "admin.delete", row["username"])
            out.emit({"deleted": row["username"]}, f"Admin user '{row['username']}' deleted.")
        else:
            raise CliError("usage: admin {add,list,passwd,remove}", "usage", 2)


def cmd_recreate(args: argparse.Namespace, out: Out) -> None:
    client = _resolve_client(args, required=False)
    client_id = client["id"] if client else None
    if args.outdated:
        names = [s["name"] for s in sandboxes.list_sandboxes(client_id)
                 if s.get("outdated") and s["status"] in ("running", "stopped", "error")]
    elif args.name:
        names = [args.name]
    else:
        raise CliError("give a sandbox name or --outdated", "usage", 2)
    results: Dict[str, str] = {}
    for name in names:
        if not out.json and not out.quiet:
            print(f"{YELLOW}Rebuilding {name} ...{NC}", flush=True)
        try:
            sandboxes.recreate(name, client_id, actor="cli", force=args.force)
            results[name] = "ok"
        except SandboxError as exc:
            results[name] = exc.message
    failed = {k: v for k, v in results.items() if v != "ok"}
    out.emit({"recreated": [k for k, v in results.items() if v == "ok"], "failed": failed},
             "\n".join(f"{GREEN if v == 'ok' else RED}{k}: {v}{NC}" for k, v in results.items())
             or "Nothing to rebuild - all sandboxes are up to date.")
    if failed:
        raise CliError(f"{len(failed)} sandbox(es) could not be rebuilt", "recreate_failed", 1)


def cmd_firewall(args: argparse.Namespace, out: Out) -> None:
    if args.action == "remove":
        netctl.remove_firewall()
        out.emit({"removed": True}, "Sandbox firewall rules removed.")
        return
    if args.action == "apply" and args.boot:
        # Runs before dockerd so the rules exist before containers come back.
        # Must not touch docker at all: the docker socket is socket-activated
        # and dockerd is ordered after this unit -> it would deadlock.
        result = netctl.apply_firewall(use_docker=False)
        out.emit(result, f"Sandbox firewall applied for {result.get('subnet', '-')} (boot mode).")
        return
    if args.action == "apply":
        netctl.ensure_network()
        netctl.apply_firewall()
    status = netctl.firewall_status()
    net = dockerctl.network_inspect(settings().network_name)
    status["network"] = settings().network_name if net else None
    status["icc"] = ((net or {}).get("Options") or {}).get("com.docker.network.bridge.enable_icc")
    offline = dockerctl.network_inspect(settings().offline_network_name)
    status["offlineNetwork"] = settings().offline_network_name if offline else "(created on first use)"
    out.emit(status, "\n".join(f"{k}: {v}" for k, v in status.items()))
    if status.get("enabled") and not status.get("ok"):
        raise CliError("sandbox firewall is not complete", "firewall", 1)


def cmd_reconcile(args: argparse.Namespace, out: Out) -> None:
    report = reconcile.reconcile(dry_run=args.dry_run, actor="cli")
    lines = [f"{'[dry-run] ' if args.dry_run else ''}{a['action']:16} {a['target']}  ({a['detail']})"
             for a in report["actions"]] or ["Nothing to do - everything is consistent."]
    lines += [f"{RED}error: {e}{NC}" for e in report["errors"]]
    out.emit(report, "\n".join(lines))
    if report["errors"]:
        raise CliError("reconcile finished with errors", "reconcile_errors", 1)


def cmd_migrate(args: argparse.Namespace, out: Out) -> None:
    db.init_db()
    report = sandboxes.migrate_legacy_registry()
    out.emit(report, f"Imported: {', '.join(report['imported']) or '-'}\n"
                     + "".join(f"Skipped {k}: {v}\n" for k, v in report["skipped"].items()))


def cmd_selfcheck(args: argparse.Namespace, out: Out) -> None:
    s = settings()
    with db.session() as conn:
        info = {
            "result": "ok",
            "dbPath": str(s.db_path),
            "schemaVersion": conn.execute("PRAGMA user_version").fetchone()[0],
            "clients": conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0],
            "sandboxes": conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0],
            "admins": admins.count(conn),
        }
    try:
        firewall_ok = netctl.firewall_status()["ok"]
    except SandboxError:
        firewall_ok = False
    info.update({
        "isolationOk": firewall_ok and dockerctl.network_inspect(s.network_name) is not None,
        "dockerReachable": dockerctl.available(),
        "image": s.docker_image,
        "imagePresent": bool(dockerctl.image_id(s.docker_image)),
        "offlineNetwork": f"{s.offline_network_name} ({s.offline_network_subnet})",
        "publicUrl": s.public_url,
        "storageDir": str(s.storage_dir),
        "nginxSandboxDir": str(s.nginx_sandbox_dir),
        "portRange": f"{s.port_range[0]}-{s.port_range[1]}",
        "defaults": {"cpus": s.default_cpus, "memoryMb": s.default_memory_mb, "diskGb": s.default_disk_gb},
        "inactivitySeconds": s.inactivity_seconds,
        "maxAgeSeconds": s.max_age_seconds,
    })
    out.emit(info, "\n".join(f"{k}: {v}" for k, v in info.items()))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out(args)
    command = args.command or "help"
    try:
        if command == "help":
            parser.print_help()
            return 0
        db.init_db()
        if command in SANDBOX_COMMANDS:
            cmd_sandbox(args, out)
        elif command == "persistent":
            cmd_persistent(args, out)
        elif command == "client":
            cmd_client(args, out)
        elif command == "admin":
            cmd_admin(args, out)
        elif command == "profile":
            cmd_profile(args, out)
        elif command == "reconcile":
            cmd_reconcile(args, out)
        elif command == "recreate":
            cmd_recreate(args, out)
        elif command == "firewall":
            cmd_firewall(args, out)
        elif command == "migrate":
            cmd_migrate(args, out)
        elif command == "mount-disks":
            report = reconcile.mount_all()
            out.emit(report, f"mounted: {len(report['mounted'])}, failed: {len(report['failed'])}")
            return 1 if report["failed"] else 0
        elif command == "watch":
            return watcher.run_once()
        elif command == "usage-refresh":
            count = sandboxes.refresh_usage()
            out.emit({"refreshed": count}, f"refreshed usage of {count} sandboxes")
        elif command == "build":
            s = settings()
            if not out.json and not out.quiet:
                print(f"{YELLOW}Building Docker image {s.docker_image}...{NC}")
            proc = dockerctl.build(s.docker_image, str(s.docker_build_dir))
            if proc.returncode != 0:
                raise CliError(f"Docker build failed with exit code {proc.returncode}", "build_failed",
                               proc.returncode)
            out.emit({"result": "built", "image": s.docker_image}, f"{GREEN}Image built successfully{NC}")
        elif command == "selfcheck":
            cmd_selfcheck(args, out)
        return 0
    except (CliError, SandboxError, dockerctl.DockerError) as exc:
        if getattr(args, "debug", False):
            raise
        if isinstance(exc, CliError):
            code, message, exit_code = exc.code, exc.message, exc.exit_code
        elif isinstance(exc, SandboxError):
            code, message = exc.code, exc.message
            exit_code = 3 if isinstance(exc, NotFound) else 1
        else:
            code, message, exit_code = "docker", str(exc), 1
        if out.json:
            print(json.dumps({"error": code, "message": message, "exitCode": exit_code}))
        else:
            print(f"{RED}Error: {message}{NC}", file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
