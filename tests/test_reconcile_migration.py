import json
import time

from albert_sandbox import db, nginxctl, ports, reconcile, sandboxes, watcher


def test_reconcile_removes_leftovers_and_dry_run_only_reports(env, make_client):
    client, _ = make_client()
    keep = sandboxes.create(client["id"])["name"]
    lost = sandboxes.create(client["id"])["name"]
    docker, disks = env["docker"], env["disks"]

    # leftovers of every kind
    docker.containers.pop(lost)                                    # sandbox lost its container
    docker.add_container("sbx-orphan-1", {"albert.manager": "1"}, created=time.time() - 3600)
    docker.add_container("sbx-fresh-1", {"albert.manager": "1"})  # within grace period -> keep
    docker.add_container("unrelated", {})                          # not ours -> keep
    docker.volumes.update({"sbx-gone_data", "other-volume"})
    disks.disks["sbx-old-disk"] = {"size": 5, "mounted": True}
    stale = nginxctl.conf_path("sbx-deleted-long-ago")
    stale.write_text("location /x/ {}\n")
    nginxctl.conf_path(keep).unlink()                              # missing route of a live sandbox

    preview = reconcile.reconcile(dry_run=True)
    kinds = {(a["action"], a["target"]) for a in preview["actions"]}
    assert ("remove-sandbox", lost) in kinds
    assert ("remove-container", "sbx-orphan-1") in kinds
    assert ("remove-disk", "sbx-old-disk") in kinds
    assert ("remove-volume", "sbx-gone_data") in kinds
    assert ("remove-route", "sbx-deleted-long-ago") in kinds
    assert ("write-route", keep) in kinds
    assert "sbx-orphan-1" in docker.containers and stale.exists()  # nothing changed yet

    report = reconcile.reconcile()
    assert report["errors"] == []
    assert "sbx-orphan-1" not in docker.containers
    assert "sbx-fresh-1" in docker.containers and "unrelated" in docker.containers
    assert "sbx-old-disk" not in disks.disks and lost not in disks.disks
    assert docker.volumes == {"other-volume"}
    assert nginxctl.list_names() == [keep]
    with db.session() as conn:
        assert [r["name"] for r in conn.execute("SELECT name FROM sandboxes")] == [keep]
    # second run: nothing left to do
    assert reconcile.reconcile()["actions"] == []


def test_reconcile_mounts_unmounted_disks(env, make_client):
    client, _ = make_client()
    name = sandboxes.create(client["id"])["name"]
    env["disks"].disks[name]["mounted"] = False
    report = reconcile.reconcile()
    assert ("mount-disk", name) in {(a["action"], a["target"]) for a in report["actions"]}
    assert env["disks"].disks[name]["mounted"]


def test_migrate_legacy_registry_keeps_running_sandbox(env, make_client):
    client, _ = make_client("Legacy")
    s = env["settings"]
    s.legacy_registry.parent.mkdir(parents=True, exist_ok=True)
    s.legacy_registry.write_text(json.dumps([
        {"name": "sbx-legacy-1", "port": "6080", "vnc_port": "5900", "mcphub_port": "7080",
         "filesvc_port": "7280", "created": "2026-11-11T16:39:52+01:00"},
        {"name": "sbx-legacy-2", "port": "6081", "vnc_port": "5901", "mcphub_port": "7081",
         "filesvc_port": "7281", "created": "2026-11-11T16:40:00+01:00", "persistent": True,
         "vnc_password": "Xy12ab34", "filesvc_token": "tok"},
        {"name": "sbx-vanished", "port": "6082"},
    ]))
    env["docker"].add_container("sbx-legacy-1", {"albert.manager": "1", "albert.apikey_hash": client["key_hash"]})
    env["docker"].add_container("sbx-legacy-2", {"albert.manager": "1", "albert.apikey_hash": client["key_hash"]},
                                running=False, host_config={"NanoCpus": 2_000_000_000, "Memory": 2 * 1024 ** 3})

    report = sandboxes.migrate_legacy_registry()
    assert sorted(report["imported"]) == ["sbx-legacy-1", "sbx-legacy-2"]
    assert report["skipped"]["sbx-vanished"] == "container missing"
    assert not s.legacy_registry.exists()

    one = sandboxes.describe("sbx-legacy-1", client["id"])
    assert one["ports"] == {"novnc": "6080", "vnc": "5900", "mcphub": "7080", "filesvc": "7280"}
    assert one["resources"]["storage"] == "volume" and one["resources"]["cpus"] is None
    two = sandboxes.describe("sbx-legacy-2", client["id"])
    assert two["persistent"] is True and two["resources"]["cpus"] == 2 and two["resources"]["memoryMb"] == 2048
    with db.session() as conn:
        assert conn.execute("SELECT vnc_password FROM sandboxes WHERE name='sbx-legacy-1'").fetchone()[0] == "albert"
        # legacy ports stay reserved
        assert {6080, 5900, 7080, 7280} <= ports.reserved_ports(conn)

    # reconcile writes routes for migrated sandboxes and keeps their volumes
    env["docker"].volumes.update({"sbx-legacy-1_data", "sbx-legacy-2_data"})
    reconcile.reconcile()
    assert set(nginxctl.list_names()) == {"sbx-legacy-1", "sbx-legacy-2"}
    assert "password=albert" in nginxctl.conf_path("sbx-legacy-1").read_text()
    assert env["docker"].volumes == {"sbx-legacy-1_data", "sbx-legacy-2_data"}

    # legacy sandboxes can be started/stopped/removed with the new code
    sandboxes.start("sbx-legacy-2", client["id"])
    sandboxes.remove("sbx-legacy-2", client["id"])
    assert "sbx-legacy-2_data" not in env["docker"].volumes

    # running the migration again is a no-op
    assert sandboxes.migrate_legacy_registry() == {"imported": [], "skipped": {}}


def test_port_allocation_skips_reserved_and_listening(env, make_client, monkeypatch):
    lo = env["settings"].port_range[0]
    monkeypatch.setattr(ports, "listening_ports", lambda: {lo, lo + 2})
    client, _ = make_client()
    result = sandboxes.create(client["id"])
    used = {int(v) for v in result["ports"].values() if v}
    assert used == {lo + 1, lo + 3, lo + 4}
    second = sandboxes.create(client["id"])
    assert not used & {int(v) for v in second["ports"].values() if v}


def test_watcher_stops_idle_and_removes_expired(env, make_client, monkeypatch):
    client, _ = make_client(allow_persistent=True)
    idle = sandboxes.create(client["id"])["name"]
    busy = sandboxes.create(client["id"])["name"]
    old = sandboxes.create(client["id"])["name"]
    keep = sandboxes.create(client["id"], persistent=True)["name"]
    now = time.time()
    with db.session() as conn:
        conn.execute("UPDATE sandboxes SET last_activity_at=?, last_started_at=?, created_at=?",
                     (int(now - 3600), int(now - 3600), int(now - 3600)))
        conn.execute("UPDATE sandboxes SET created_at=? WHERE name IN (?, ?)", (int(now - 200000), old, keep))
    for name in (idle, busy, old, keep):
        env["docker"].containers[name]["State"]["StartedAt"] = "2020-01-01T00:00:00Z"
    stamp = time.strftime("%d/%b/%Y:%H:%M:%S %z", time.localtime(now - 10))
    env["settings"].nginx_access_log.write_text(
        f'1.2.3.4 - - [{stamp}] "GET /{busy}/websockify HTTP/1.1" 101 0 "-" "x"\n')
    monkeypatch.setattr(watcher, "established_ports", lambda: set())

    watcher.update_activity_and_stop_idle(now)
    watcher.remove_expired(now)

    running = env["docker"].running_names()
    assert idle not in running and busy in running
    with db.session() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sandboxes")}
    assert old not in names and keep in names
