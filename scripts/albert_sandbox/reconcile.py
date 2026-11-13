"""Bring Docker, nginx and disk images back in line with the database.

The database is the source of truth. Everything the manager created on the
host that has no matching sandbox row is removed, and every sandbox row that
lost its container is cleaned up completely. The function is idempotent and
runs every minute from the watcher; ``dry_run`` only reports what it would do.
"""
from __future__ import annotations

import re
import shutil
import time
from typing import Any, Dict, List

from . import db, dockerctl, netctl, nginxctl, profiles, storage
from .config import settings
from .errors import SandboxError
from .locking import host_lock
from .sandboxes import (STATE_CREATING, STATE_ERROR, STATE_READY, STATE_REBUILDING, STATE_REMOVING, _destroy,
                        parse_time, recreate)

LEGACY_VOLUME_RE = re.compile(r"^(sbx-[a-z0-9.-]+)_data$")


def _db_names() -> set:
    with db.session() as conn:
        return {r["name"] for r in conn.execute("SELECT name FROM sandboxes")}


def reconcile(dry_run: bool = False, actor: str = "reconcile") -> Dict[str, Any]:
    s = settings()
    now = time.time()
    actions: List[Dict[str, str]] = []
    errors: List[str] = []

    def act(action: str, target: str, detail: str = "") -> None:
        actions.append({"action": action, "target": target, "detail": detail})

    docker_names = set(dockerctl.managed_container_names())
    infos = dockerctl.inspect(docker_names)

    # 0: network isolation first (security): isolated network, firewall rules,
    # and every managed container inside the isolated network only.
    try:
        if dockerctl.network_inspect(s.network_name) is None:
            act("create-network", s.network_name, "isolated sandbox network missing")
            if not dry_run:
                netctl.ensure_network()
        if s.firewall and not netctl.firewall_status()["ok"]:
            act("apply-firewall", "iptables", "sandbox firewall rules missing or outdated")
            if not dry_run:
                netctl.apply_firewall()
    except SandboxError as exc:
        errors.append(exc.message)
    # Network per sandbox follows the owner's internet access; containers
    # unknown to the database get the most restrictive (offline) network.
    with db.session() as conn:
        policy = {r["name"]: (not r["allow_internet"], r["state"]) for r in conn.execute(
            "SELECT s.name, s.state, k.allow_internet FROM sandboxes s JOIN api_keys k ON k.id = s.api_key_id")}
    for name in sorted(docker_names):
        info = infos.get(name)
        offline = policy.get(name, (True, None))[0]
        if info and not netctl.is_isolated(info, offline):
            act("isolate-network", name,
                f"container not (only) in the {'offline' if offline else 'isolated'} network: "
                + ",".join(netctl.container_networks(info)))
            if not dry_run:
                try:
                    netctl.isolate(name, info, offline)
                except (dockerctl.DockerError, SandboxError) as exc:
                    errors.append(str(exc))
    # Running sandboxes whose DNS setting does not match the internet access
    # (normally fixed right when the setting changes) are rebuilt; stopped ones
    # are rebuilt when they are started.
    for name in sorted(docker_names):
        info = infos.get(name)
        offline, state = policy.get(name, (None, None))
        if not info or offline is None or state != STATE_READY or not (info.get("State") or {}).get("Running"):
            continue
        if not netctl.dns_matches(info, offline):
            act("rebuild-network", name, "DNS setting does not match the client's internet access")
            if not dry_run:
                try:
                    recreate(name, actor=actor)
                except SandboxError as exc:
                    errors.append(exc.message)

    with db.session() as conn:
        rows = conn.execute("SELECT * FROM sandboxes").fetchall()
        db_names = {r["name"] for r in rows}

        # 1-3: sandbox rows that need to be torn down
        for row in rows:
            name = row["name"]
            reason = None
            if row["state"] == STATE_REMOVING:
                reason = "finish interrupted removal"
            elif row["state"] == STATE_CREATING and now - row["created_at"] > s.orphan_grace_seconds:
                reason = "creation never finished"
            elif row["state"] == STATE_READY and name not in docker_names \
                    and dockerctl.inspect_one(name) is None:
                # (re-checked: the container may have been created after the snapshot)
                reason = "container no longer exists"
            if not reason:
                continue
            act("remove-sandbox", name, reason)
            if not dry_run:
                try:
                    _destroy(conn, row, actor=actor, reason=reason)
                    db_names.discard(name)
                except SandboxError as exc:
                    errors.append(exc.message)

        live_rows = [r for r in conn.execute("SELECT * FROM sandboxes WHERE state=?", (STATE_READY,))]

    # 4: containers labelled as ours without a database row
    for name in sorted(docker_names - db_names):
        created = parse_time((infos.get(name) or {}).get("Created")) or 0
        if now - created < s.orphan_grace_seconds:
            continue
        act("remove-container", name, "container not registered in database")
        if not dry_run:
            try:
                dockerctl.remove(name)
                dockerctl.volume_remove(f"{name}_data")
            except dockerctl.DockerError as exc:
                errors.append(str(exc))

    # 5: disk images / mount points without a sandbox. List the disks first
    # and read the database afterwards: a sandbox row is always inserted
    # before its disk is created, so a concurrent create is never mistaken
    # for garbage.
    disks = storage.list_names()
    current_names = _db_names()
    for name, parts in sorted(disks.items()):
        if name in current_names:
            continue
        act("remove-disk", name, "disk image without sandbox: " + ",".join(sorted(parts)))
        if not dry_run:
            try:
                storage.remove(name)
            except storage.StorageError as exc:
                errors.append(str(exc))

    # 6: legacy docker volumes (<name>_data) without a sandbox
    try:
        volumes = dockerctl.volume_names()
    except dockerctl.DockerError:
        volumes = []
    legacy_in_use = {r["name"] for r in live_rows if r["storage"] == "volume"}
    for volume in volumes:
        match = LEGACY_VOLUME_RE.match(volume)
        if not match:
            continue
        owner = match.group(1)
        if owner in legacy_in_use or owner in docker_names:
            continue
        act("remove-volume", volume, "sandbox data volume without sandbox")
        if not dry_run and not dockerctl.volume_remove(volume):
            errors.append(f"could not remove volume {volume}")

    # 7: nginx routes - exactly one per live sandbox
    nginx_changed = False
    with host_lock():
        # Routes are only written under the host lock, so a fresh read here is consistent.
        current_names = _db_names()
        for name in nginxctl.list_names():
            if name not in current_names:
                act("remove-route", name, "nginx route without sandbox")
                if not dry_run:
                    nginx_changed |= nginxctl.remove(name)
        for stray in nginxctl.stray_files():
            act("remove-file", str(stray), "stray file in managed nginx directory")
            if not dry_run:
                stray.unlink(missing_ok=True)
        for row in live_rows:
            if row["name"] not in docker_names:
                continue
            path = nginxctl.conf_path(row["name"])
            expected = nginxctl.render(row["name"], row["novnc_port"], row["mcphub_port"],
                                       row["filesvc_port"], row["vnc_password"] or "")
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != expected:
                act("write-route", row["name"], "route missing" if current is None else "route outdated")
                if not dry_run:
                    nginx_changed |= nginxctl.write(row)
        if nginx_changed:
            try:
                nginxctl.reload()
            except nginxctl.NginxError as exc:
                errors.append(str(exc))

    # 8: make sure disk images of live sandboxes are mounted (e.g. after reboot)
    for row in live_rows:
        if row["storage"] == "image" and storage.exists(row["name"]) and not storage.is_mounted(row["name"]):
            act("mount-disk", row["name"], "disk image was not mounted")
            if not dry_run:
                try:
                    storage.mount(row["name"])
                except storage.StorageError as exc:
                    errors.append(str(exc))

    # 9: sandbox profiles - interrupted builds, images nobody uses any more,
    # build directories of deleted profiles
    with db.session() as conn:
        stale = conn.execute(
            "SELECT name FROM profiles WHERE build_state IN ('queued','building') "
            "AND COALESCE(build_heartbeat_at, build_started_at, 0) < ?",
            (int(now) - profiles.STALE_SECONDS,)).fetchall()
        for row in stale:
            act("fail-build", row["name"], "build job stopped sending heartbeats")
        if stale and not dry_run:
            profiles.expire_stale(conn)
    try:
        used_ids = {(info or {}).get("Image") for info in dockerctl.inspect(dockerctl.managed_container_names()).values()}
        for image in profiles.unused_images(i for i in used_ids if i):
            act("remove-image", image["ref"], "profile image not used any more")
            if not dry_run and not dockerctl.image_remove(image["ref"]):
                errors.append(f"could not remove image {image['ref']}")
    except dockerctl.DockerError as exc:
        errors.append(str(exc))
    for path in profiles.stray_dirs():
        act("remove-profile-dir", str(path), "build files of a deleted profile")
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)

    if actions and not dry_run:
        with db.session() as conn:
            db.audit(conn, actor, "reconcile", None, {"actions": actions[:200], "errors": errors[:50]})
    return {"dryRun": dry_run, "actions": actions, "errors": errors}


def mount_all() -> Dict[str, Any]:
    """Mount all sandbox disks (used at boot, before Docker starts containers)."""
    mounted, failed = [], {}
    with db.session() as conn:
        rows = conn.execute("SELECT name FROM sandboxes WHERE storage='image' AND state IN (?, ?, ?, ?)",
                            (STATE_READY, STATE_CREATING, STATE_REBUILDING, STATE_ERROR)).fetchall()
    for row in rows:
        name = row["name"]
        if storage.is_mounted(name) or not storage.exists(name):
            continue
        try:
            storage.mount(name)
            mounted.append(name)
        except storage.StorageError as exc:
            failed[name] = str(exc)
    return {"mounted": mounted, "failed": failed}
