"""Profiles captured from a running sandbox: package comparison, APT sources,
hints, proposals of ALBERT | AI and their acceptance by an administrator."""
import base64
import json
import shlex

import pytest

from albert_sandbox import capture, clients, db, dockerctl, profiles, sandboxes, web
from albert_sandbox.errors import BadRequest

BASE = """@@apt
bash
nano
python3
@@installed
ii |bash|5.2
ii |nano|7.2
ii |python3|3.12
@@pip
[{"name": "Babel", "version": "2.10.3", "location": "/usr/lib/python3/dist-packages", "installer": ""},
 {"name": "pandas", "version": "2.0.0", "location": "/usr/local/lib/python3.12/dist-packages", "installer": "pip"}]

@@npm
{"dependencies": {"npm": {"version": "11.20.0"}}}

@@npmuser

@@sources
FILE aaa /etc/apt/sources.list.d/ubuntu.sources
@@end
"""

SANDBOX = """@@apt
bash
python3
cowsay
jq
bad;name
@@installed
ii |bash|5.2
rc |nano|7.2
ii |python3|3.12
ii |cowsay|3.03
ii |jq|1.7
ii |libjq1:amd64|1.7
@@pip
[{"name": "Babel", "version": "2.10.3", "location": "/usr/lib/python3/dist-packages", "installer": ""},
 {"name": "pandas", "version": "2.2.3", "location": "/usr/local/lib/python3.12/dist-packages", "installer": "pip"},
 {"name": "humanize", "version": "4.11.0", "location": "/home/ubuntu/.local/lib/python3.12/site-packages",
  "installer": "pip"},
 {"name": "numpy", "version": "1.26.4", "location": "/usr/lib/python3/dist-packages", "installer": "debian"}]

@@npm
{"dependencies": {"npm": {"version": "11.20.0"}, "typescript": {"version": "5.6.3"}}}

@@npmuser

@@sources
FILE aaa /etc/apt/sources.list.d/ubuntu.sources
FILE bbb /etc/apt/sources.list.d/github-cli.list
FILE ccc /etc/apt/keyrings/gh.gpg
BIG /usr/share/keyrings/huge.gpg
@@end
"""

SOURCE_LIST = b"deb [signed-by=/etc/apt/keyrings/gh.gpg] https://cli.github.com/packages stable main\n"
KEY = bytes(range(256))

DIFF = [("A", "/opt/myapp"), ("A", "/opt/myapp/run.sh"), ("C", "/etc"), ("C", "/etc/bash.bashrc"),
        ("A", "/usr/bin/jq"), ("A", "/usr/bin/tsc"), ("A", "/usr/local/bin/humanize"), ("A", "/tmp/x"),
        ("D", "/usr/bin/nano"), ("A", "/etc/ld.so.cache"),
        ("A", "/usr/local/lib/python3.12/dist-packages/tabulate"),
        ("A", "/usr/local/lib/python3.12/dist-packages/tabulate-0.10.0.dist-info")]
CLASSES = {
    "/opt/myapp": ["d", "", False, ""],
    "/opt/myapp/run.sh": ["f", "", False, ""],
    "/etc": ["d", "base-files", False, ""],
    "/etc/bash.bashrc": ["f", "bash", False, ""],
    "/usr/bin/jq": ["f", "jq", False, ""],
    "/usr/bin/tsc": ["l", "", False, "../lib/node_modules/typescript/bin/tsc"],
    "/usr/local/bin/humanize": ["f", "", True, ""],
}


@pytest.fixture
def inspect(env, monkeypatch):
    """Fake inspection commands; ``state`` changes what the sandbox reports."""
    state = {"sandbox": SANDBOX, "base": BASE, "diff": list(DIFF), "classes": dict(CLASSES),
             "files": {"/etc/apt/sources.list.d/github-cli.list": SOURCE_LIST, "/etc/apt/keyrings/gh.gpg": KEY},
             "classified": []}

    def exec_capture(container, command, user="ubuntu", input_bytes=None, timeout=120, max_bytes=0):
        assert user == "ubuntu"  # never as root
        if command[:2] == ["bash", "-c"] and command[2] == capture.COLLECT_SCRIPT:
            return state["sandbox"].encode()
        if command[:2] == ["bash", "-c"] and command[2] == capture.READ_SCRIPT:
            lines = [f"{base64.b64encode(state['files'][p]).decode()} {p}" for p in command[4:] if p in state["files"]]
            return ("\n".join(lines) + "\n").encode()
        if command[0] == "python3":
            asked = json.loads(input_bytes)
            state["classified"].append(asked)
            return json.dumps({p: state["classes"].get(p, ["f", "", False, ""]) for p in asked}).encode()
        raise AssertionError(command)

    def run_capture(image, command, user="ubuntu", timeout=180, max_bytes=0):
        state["base_image"] = image
        return state["base"].encode()

    monkeypatch.setattr(dockerctl, "exec_capture", exec_capture)
    monkeypatch.setattr(dockerctl, "run_capture", run_capture)
    monkeypatch.setattr(dockerctl, "diff", lambda name: list(state["diff"]))
    return state


@pytest.fixture
def app(env, monkeypatch):
    monkeypatch.setattr(web, "throttle", web._LoginThrottle())
    application = web.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def admin(app):
    client = app.test_client()
    client.environ_base["HTTP_X_ALBERT_ADMIN_GATE"] = "1"
    from albert_sandbox import admins
    with db.session() as conn:
        admins.create(conn, "cap-admin", "sehr-geheim-123")
    data = client.post("/admin/api/login", json={"username": "cap-admin", "password": "sehr-geheim-123"})
    client.csrf = {"X-CSRF-Token": data.get_json()["csrf"]}
    return client


def _sandbox(client_id, owner="u1", profile=None):
    return sandboxes.create(client_id, requested_name="cap", actor="test", owner=owner, profile=profile)["name"]


def _api(app, key, user="u1"):
    http = app.test_client()
    return http, {"Authorization": f"Bearer {key}", "X-Albert-User": user}


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def test_capture_finds_new_packages_sources_and_hints(env, make_client, inspect):
    client, _ = make_client()
    name = _sandbox(client["id"])
    result = capture.capture(name, "sha256:img")
    assert inspect["base_image"] == "sha256:img"
    assert result["packages"] == {"apt": ["cowsay", "jq"], "pip": ["humanize==4.11.0", "pandas==2.2.3"],
                                  "npm": ["typescript@5.6.3"]}
    assert result["removedPackages"] == ["nano"]  # only "rc" left: removed
    assert result["aptSources"] == ["/etc/apt/keyrings/gh.gpg", "/etc/apt/sources.list.d/github-cli.list"]
    assert "apt: bad;name" in result["skipped"]
    assert any("huge.gpg" in s for s in result["skipped"])
    assert result["hints"] == [
        {"path": "/etc/bash.bashrc", "change": "changed", "kind": "file", "files": 0},
        {"path": "/opt/myapp", "change": "added", "kind": "dir", "files": 1},
    ]
    # runtime noise is never sent for classification
    assert all(not p.startswith(("/tmp", "/etc/ld.so.cache", "/usr/local/lib/python3"))
               for p in inspect["classified"][0])


def test_prepare_script_quotes_everything(env, make_client, inspect):
    hostile = b"deb https://example.org/$(touch /pwned) stable main\n# it's `id` and \\n\nline\twith tab\n"
    script = capture.prepare_script({"/etc/apt/sources.list.d/x.list": hostile,
                                     "/etc/apt/keyrings/x.gpg": KEY}, "sbx-test")
    lines = script.splitlines()
    key_line = next(line for line in lines if "x.gpg" in line and "base64" in line)
    list_line = next(line for line in lines if line.startswith("printf '%s\\n'"))
    assert lines.index(key_line) < lines.index(list_line)  # keys before the sources using them
    words = shlex.split(list_line)
    assert words[:2] == ["printf", "%s\\n"] and words[-2:] == [">", "/etc/apt/sources.list.d/x.list"]
    assert "\n".join(words[2:-2]) + "\n" == hostile.decode()  # literally, nothing expanded
    words = shlex.split(key_line)
    assert base64.b64decode(words[2]) == KEY


def test_unusual_source_paths_are_skipped(env, make_client, inspect):
    inspect["sandbox"] = SANDBOX.replace("FILE bbb /etc/apt/sources.list.d/github-cli.list",
                                         "FILE bbb /etc/apt/sources.list.d/../../../root/x")
    client, _ = make_client()
    result = capture.capture(_sandbox(client["id"]), "sha256:img")
    assert result["aptSources"] == ["/etc/apt/keyrings/gh.gpg"]
    assert any("unusual file name" in s for s in result["skipped"])


def test_merge_keeps_the_profile_and_replaces_versions(env, make_client, inspect):
    with db.session() as conn:
        base = profiles.create(conn, "Daten", apt_packages="libreoffice jq", pip_packages="pandas==2.0.0",
                               setup_script="echo hi", prepare_script="echo prep")
    captured = capture.capture(_sandbox(make_client()[0]["id"]), "sha256:img")
    fields = capture.merge(base, captured)
    assert fields["apt_packages"].split("\n") == ["libreoffice", "jq", "cowsay"]
    assert fields["pip_packages"].split("\n") == ["pandas==2.2.3", "humanize==4.11.0"]
    assert fields["setup_script"] == "echo hi"
    assert fields["prepare_script"].startswith("echo prep\n\n# APT sources")


# --------------------------------------------------------------------------
# build: preparation before the packages; old profiles keep their hash
# --------------------------------------------------------------------------

def test_prepare_runs_before_the_packages(env):
    with db.session() as conn:
        row = profiles.create(conn, "Vendor", apt_packages="gh", prepare_script="echo prepare")
    profiles.request_build(row["id"], actor="test")
    build = env["docker"].builds[-1]
    assert build["prepare"] == "echo prepare\n"
    text = build["dockerfile"]
    assert text.index("albert-profile-prepare.sh") < text.index("apt-get install")


def test_profiles_without_preparation_keep_their_hash(env):
    with db.session() as conn:
        row = profiles.create(conn, "Alt", apt_packages="jq", setup_script="echo x")
    old = json.dumps([profiles.TEMPLATE_VERSION, profiles.packages(row), "echo x"], sort_keys=True)
    import hashlib
    assert profiles.config_hash(row) == hashlib.sha256(old.encode()).hexdigest()


# --------------------------------------------------------------------------
# client API: proposals
# --------------------------------------------------------------------------

def test_client_preview_and_proposal(app, make_client, inspect):
    client, key = make_client(allow_profile_proposals=True)
    name = _sandbox(client["id"])
    http, headers = _api(app, key)
    health = http.get("/health", headers=headers).get_json()
    assert "profile-proposals" in health["features"]

    r = http.post(f"/containers/{name}/profile", json={"preview": True}, headers=headers)
    assert r.status_code == 200
    preview = r.get_json()["preview"]
    assert preview["basedOn"] == "standard" and preview["captured"]["apt"] == ["cowsay", "jq"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0  # preview saves nothing

    r = http.post(f"/containers/{name}/profile", json={"description": "x"}, headers=headers)
    assert r.status_code == 400  # name required
    r = http.post(f"/containers/{name}/profile", json={"name": "standard"}, headers=headers)
    assert r.status_code == 400

    r = http.post(f"/containers/{name}/profile", json={"name": "Werkzeuge", "description": "jq und Co."},
                  headers=headers)
    assert r.status_code == 201, r.get_json()
    proposal = r.get_json()["proposal"]
    assert proposal["state"] == "proposed" and proposal["name"] == "Werkzeuge"
    assert "id" not in proposal

    listing = http.get("/profiles", headers=headers).get_json()
    assert [p["name"] for p in listing["profiles"]] == ["standard"]  # not offered before it is accepted
    assert [p["name"] for p in listing["proposals"]] == ["Werkzeuge"]
    assert "cowsay" in listing["proposals"][0]["packages"]

    r = http.post(f"/containers/{name}/profile", json={"name": "werkzeuge"}, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (409, "profile_name_taken")

    with db.session() as conn:
        row = profiles.find(conn, "Werkzeuge")
        assert row["proposal"] == 1 and row["origin_client_id"] == client["id"]
        assert row["origin_owner"] == "u1" and row["origin_sandbox"] == name
        assert row["image"] is None  # never built by itself
        audit = conn.execute("SELECT action FROM audit_log WHERE target='Werkzeuge'").fetchall()
        assert [a["action"] for a in audit] == ["profile.propose"]


def test_proposal_limit_and_access(app, make_client, inspect, env):
    client, key = make_client(allow_profile_proposals=True)
    name = _sandbox(client["id"])
    http, headers = _api(app, key)
    for i in range(profiles.MAX_OPEN_PROPOSALS):
        assert http.post(f"/containers/{name}/profile", json={"name": f"P{i}"}, headers=headers).status_code == 201
    r = http.post(f"/containers/{name}/profile", json={"name": "zu viel"}, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (409, "proposal_limit")
    assert r.get_json()["limit"] == profiles.MAX_OPEN_PROPOSALS
    # another ALBERT user of the same client does not see the sandbox
    _, other = _api(app, key, user="u2")
    assert http.post(f"/containers/{name}/profile", json={"name": "fremd"}, headers=other).status_code == 404
    # stopped sandbox
    env["docker"].containers[name]["State"]["Running"] = False
    with db.session() as conn:
        conn.execute("DELETE FROM profiles")
    r = http.post(f"/containers/{name}/profile", json={"name": "aus"}, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (409, "sandbox_not_running")


def test_nothing_new_is_refused(app, make_client, inspect):
    inspect["sandbox"] = BASE
    client, key = make_client(allow_profile_proposals=True)
    name = _sandbox(client["id"])
    http, headers = _api(app, key)
    r = http.post(f"/containers/{name}/profile", json={"name": "leer"}, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (400, "no_changes")
    assert r.get_json()["hints"]  # tells what changed outside the package management


# --------------------------------------------------------------------------
# administrator: accept, reject, capture
# --------------------------------------------------------------------------

def _propose(app, key, name, profile_name="Werkzeuge"):
    http, headers = _api(app, key)
    r = http.post(f"/containers/{name}/profile", json={"name": profile_name}, headers=headers)
    assert r.status_code == 201
    with db.session() as conn:
        return profiles.find(conn, profile_name)


def test_accept_allows_and_builds(app, admin, make_client, inspect, env):
    client, key = make_client(allow_profile_proposals=True)
    name = _sandbox(client["id"])
    row = _propose(app, key, name)
    with db.session() as conn:
        with pytest.raises(BadRequest) as exc:
            clients.update(conn, client["id"], {"allowed_profile_ids": [row["id"]]})
        assert exc.value.code == "profile_is_proposal"
    overview = admin.get("/admin/api/overview").get_json()
    assert overview["profileProposals"] == 1
    detail = admin.get(f"/admin/api/profiles/{row['id']}").get_json()["profile"]
    assert detail["proposal"] is True and detail["origin"]["clientId"] == client["id"]
    assert detail["capture"]["hints"][0]["path"] == "/etc/bash.bashrc"
    assert "albert-profile-prepare.sh" in detail["dockerfile"]

    r = admin.post(f"/admin/api/profiles/{row['id']}/accept", headers=admin.csrf)
    assert r.status_code == 202, r.get_json()
    data = r.get_json()["profile"]
    assert data["proposal"] is False and data["status"] == "ready"
    assert env["docker"].builds[-1]["prepare"].startswith("# APT sources")
    http, headers = _api(app, key)
    listing = http.get("/profiles", headers=headers).get_json()
    assert [p["name"] for p in listing["profiles"]] == ["standard", "Werkzeuge"]
    assert listing["proposals"] == []
    r = admin.post(f"/admin/api/profiles/{row['id']}/accept", headers=admin.csrf)
    assert r.status_code == 400  # not a proposal any more


def test_reject_deletes_the_proposal(app, admin, make_client, inspect):
    client, key = make_client(allow_profile_proposals=True)
    row = _propose(app, key, _sandbox(client["id"]))
    assert admin.delete(f"/admin/api/profiles/{row['id']}", headers=admin.csrf).status_code == 200
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
        actions = [a["action"] for a in conn.execute("SELECT action FROM audit_log WHERE target='Werkzeuge'")]
    assert actions == ["profile.propose", "profile.reject"]


def test_admin_capture_creates_a_normal_profile(app, admin, make_client, inspect):
    client, _ = make_client()
    name = _sandbox(client["id"])
    r = admin.post(f"/admin/api/sandboxes/{name}/capture", json={"preview": True}, headers=admin.csrf)
    assert r.status_code == 200 and r.get_json()["preview"]["captured"]["npm"] == ["typescript@5.6.3"]
    r = admin.post(f"/admin/api/sandboxes/{name}/capture", json={"name": "Aus Sandbox"}, headers=admin.csrf)
    assert r.status_code == 201
    data = r.get_json()["profile"]
    assert data["proposal"] is False and data["status"] == "new" and data["clients"] == []
    assert data["origin"]["sandbox"] == name and data["description"].startswith("cowsay, jq")


def test_client_deletion_keeps_proposal_acceptable(app, admin, make_client, inspect):
    client, key = make_client(allow_profile_proposals=True)
    name = _sandbox(client["id"])
    row = _propose(app, key, name)
    sandboxes.remove(name, actor="test")
    sandboxes.delete_client(client["id"], actor="test")
    detail = admin.get(f"/admin/api/profiles/{row['id']}").get_json()["profile"]
    assert detail["origin"]["clientDeleted"] is True
    assert admin.post(f"/admin/api/profiles/{row['id']}/accept", headers=admin.csrf).status_code == 202


def test_upgrade_adds_the_capture_columns(env):
    import sqlite3
    with db.session() as conn:
        row = profiles.create(conn, "Bestand", apt_packages="jq")
    conn = sqlite3.connect(str(env["settings"].db_path))
    for column in db.PROFILE_V10_COLUMNS:
        conn.execute(f"ALTER TABLE profiles DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        upgraded = profiles.get(c, row["id"])
        assert upgraded["prepare_script"] == "" and upgraded["proposal"] == 0
        assert upgraded["origin_sandbox"] is None
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# --------------------------------------------------------------------------
# permission per client, working directory
# --------------------------------------------------------------------------

def test_proposals_need_the_switch(app, make_client, inspect):
    client, key = make_client()  # new clients: not allowed
    name = _sandbox(client["id"])
    http, headers = _api(app, key)
    quota = http.get("/quota", headers=headers).get_json()
    assert quota["profileProposalsAllowed"] is False and quota["workDir"] == "/tmp/albert-files"
    for body in ({"preview": True}, {"name": "Werkzeuge"}):
        r = http.post(f"/containers/{name}/profile", json=body, headers=headers)
        assert (r.status_code, r.get_json()["error"]) == (403, "profile_proposals_not_allowed")
    assert inspect["classified"] == []  # nothing was inspected
    with db.session() as conn:
        clients.update(conn, client["id"], {"allow_profile_proposals": True})
    assert http.get("/quota", headers=headers).get_json()["profileProposalsAllowed"] is True
    assert http.post(f"/containers/{name}/profile", json={"preview": True}, headers=headers).status_code == 200


def test_admin_switch_and_upgrade_default(admin, make_client, env):
    import sqlite3
    client, _ = make_client()
    r = admin.patch(f"/admin/api/clients/{client['id']}", json={"allowProfileProposals": True}, headers=admin.csrf)
    assert r.status_code == 200 and r.get_json()["client"]["allowProfileProposals"] is True
    conn = sqlite3.connect(str(env["settings"].db_path))
    conn.execute("ALTER TABLE api_keys DROP COLUMN allow_profile_proposals")
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()
    db.init_db()
    with db.session() as c:
        assert clients.proposals_allowed(clients.get(c, client["id"])) is False  # off after the update


def test_sandbox_object_names_the_work_dir(env, make_client):
    client, _ = make_client()
    name = _sandbox(client["id"])
    assert sandboxes.describe(name)["workDir"] == "/tmp/albert-files"


def test_mcp_tools_tell_the_work_dir():
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "docker" / "mcp_settings.json"
    tools = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["bash-mcp"]["tools"]
    for name in ("run", "run_background"):
        assert "/tmp/albert-files" in tools[name]["description"]


def test_pip_installs_next_to_distribution_packages(env):
    with db.session() as conn:
        row = profiles.create(conn, "Plot", pip_packages="numpy==2.5.5 matplotlib==3.11.2")
    assert "pip3 install --no-cache-dir --ignore-installed numpy==2.5.5 matplotlib==3.11.2" in profiles.dockerfile(row)


def test_failed_build_of_an_accepted_proposal_stays_visible(app, admin, make_client, inspect, env):
    client, key = make_client(allow_profile_proposals=True)
    row = _propose(app, key, _sandbox(client["id"]))
    env["docker"].fail_build = "pip failed"
    assert admin.post(f"/admin/api/profiles/{row['id']}/accept", headers=admin.csrf).status_code == 202
    http, headers = _api(app, key)
    listing = http.get("/profiles", headers=headers).get_json()
    assert [p["name"] for p in listing["profiles"]] == ["standard"]  # cannot be chosen
    failed = listing["proposals"]
    assert [(p["name"], p["state"]) for p in failed] == [("Werkzeuge", "failed")]
    assert "administrator" in failed[0]["message"] and "pip failed" not in failed[0]["message"]
    assert failed[0]["failedAt"]
    # rebuilt successfully: an ordinary profile to choose from
    env["docker"].fail_build = None
    assert admin.post(f"/admin/api/profiles/{row['id']}/build", headers=admin.csrf).status_code == 202
    listing = http.get("/profiles", headers=headers).get_json()
    assert [p["name"] for p in listing["profiles"]] == ["standard", "Werkzeuge"] and listing["proposals"] == []
