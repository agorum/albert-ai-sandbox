"""Host capacity: memory guard for running sandboxes and rate limits of the client API."""
import pytest

from albert_sandbox import config, db, nginxctl, sandboxes, web
from albert_sandbox.errors import HostFull

GB = 1024


@pytest.fixture
def small_host(env, monkeypatch):
    """A host with 8 GB RAM: guard limit = 2 x (8192 - 1024) = 14336 MB = 7 sandboxes of 2 GB."""
    monkeypatch.setattr(sandboxes, "host_memory_mb", lambda: 8 * GB)
    return env


def _create(client, user="u1", **kw):
    return sandboxes.create(client["id"], owner=user, **kw)["name"]


def test_host_full_refuses_create_and_names_own_sandboxes(small_host, make_client):
    client, _ = make_client()
    other, _ = make_client("Anderer")
    names = [_create(client) for _ in range(6)]
    _create(other, user="fremd")  # 7 x 2 GB = 14336 MB, exactly the limit
    with pytest.raises(HostFull) as exc:
        _create(client)
    err = exc.value
    assert err.status == 503 and err.code == "host_full"
    assert err.details["limit"] == 14336 and err.details["used"] == 14336 and err.details["requested"] == 2048
    assert sorted(err.details["runningSandboxes"]) == sorted(names)  # only own ones, not the other client's
    assert "fully booked" in err.message and names[0] in err.message
    # a smaller sandbox does not fit either; after a stop there is room again
    sandboxes.stop(names[0])
    assert _create(client)


def test_start_and_more_ram_are_checked_too(small_host, make_client):
    client, _ = make_client()
    names = [_create(client) for _ in range(7)]
    sandboxes.stop(names[0])
    _create(client)  # takes the freed room
    with pytest.raises(HostFull):
        sandboxes.start(names[0])
    with pytest.raises(HostFull):
        sandboxes.update_resources(names[1], memory_mb=3 * GB)
    sandboxes.update_resources(names[1], memory_mb=GB)  # less is always fine


def test_safety_net_when_the_host_is_short_of_memory(small_host, make_client, monkeypatch):
    client, _ = make_client()
    monkeypatch.setattr(sandboxes, "host_available_mb", lambda: 900)
    with pytest.raises(HostFull) as exc:
        _create(client)
    assert "short of memory right now (900 MB free)" in exc.value.message


def test_guard_can_be_switched_off_and_counts_sandboxes_without_limit(small_host, make_client):
    client, _ = make_client()
    names = [_create(client) for _ in range(7)]
    with db.session() as conn:  # migrated sandbox without RAM limit counts with the default
        conn.execute("UPDATE sandboxes SET memory_mb=NULL WHERE name=?", (names[0],))
        guard = sandboxes.memory_guard(conn, set(names))
    assert guard["committedMb"] == 7 * 2048 and guard["limitMb"] == 14336 and guard["factor"] == 2.0
    config.configure(memory_overcommit=0.0)
    assert _create(client)
    assert sandboxes.host_overview()["memoryGuard"] is None


def test_client_api_answers_503_host_full(small_host, make_client, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    app = web.create_app()
    app.config["TESTING"] = True
    http = app.test_client()
    client, key = make_client()
    headers = {"Authorization": f"Bearer {key}", "X-Albert-User": "u1"}
    for _ in range(7):
        assert http.post("/containers", json={}, headers=headers).status_code == 201
    full = http.post("/containers", json={}, headers=headers)
    assert full.status_code == 503 and full.get_json()["error"] == "host_full"
    overview = sandboxes.host_overview()["memoryGuard"]
    assert overview["committedMb"] == 14336 and overview["limitMb"] == 14336 and overview["availableMb"] == 60000


def test_sandboxes_are_the_first_victims_of_the_oom_killer(env, make_client):
    client, _ = make_client()
    _create(client)
    args = env["docker"].run_calls[-1]
    assert args[args.index("--oom-score-adj") + 1] == "500"


# --------------------------------------------------------------------------
# rate limits
# --------------------------------------------------------------------------

def test_client_api_is_limited_per_key_and_per_address(env):
    site = nginxctl.render_site(None, [])
    assert "limit_req_zone $http_authorization zone=albert_api_key:10m rate=20r/s;" in site
    assert "limit_req_zone $binary_remote_addr zone=albert_api_ip:10m rate=40r/s;" in site
    manager = site.split("location /manager/ {", 1)[1].split("}", 1)[0]
    assert "limit_req zone=albert_api_key burst=100 nodelay;" in manager
    assert "limit_req zone=albert_api_ip burst=200 nodelay;" in manager
    assert "error_page 429 = @albert_rate_limited;" in manager
    named = site.split("location @albert_rate_limited {", 1)[1].split("\n    }", 1)[0]
    assert "add_header Retry-After 1 always;" in named and '"error": "rate_limited"' in named
    # the admin UI keeps its own limit per address
    admin = site.split(f"listen {env['settings'].admin_port}", 1)[1]
    assert "limit_req zone=albert_manager burst=40 nodelay;" in admin and "albert_api_key" not in admin


def test_rate_limits_follow_the_configuration(env):
    config.configure(api_rate_per_key=50, api_rate_per_ip=0)
    site = nginxctl.render_site(None, [])
    assert "zone=albert_api_key:10m rate=50r/s;" in site and "albert_api_ip" not in site
    assert "limit_req zone=albert_api_key burst=250 nodelay;" in site
    config.configure(api_rate_per_key=0, api_rate_per_ip=0)
    site = nginxctl.render_site(None, [])
    manager = site.split("location /manager/ {", 1)[1].split("}", 1)[0]
    assert "limit_req" not in manager and "error_page 429" not in manager
