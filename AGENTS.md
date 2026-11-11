# Repository Guidelines


## Project Structure & Module Organization
- `scripts/albert_sandbox/` is the manager core (Python package): `sandboxes.py` lifecycle, `clients.py` clients/quotas, `reconcile.py` cleanup, `watcher.py` housekeeping, `web.py` REST API + admin API, `cli.py` CLI, `storage.py` disk images, `nginxctl.py` routes, `netctl.py` network isolation (sandbox network + iptables), `dockerctl.py` docker CLI wrapper, `db.py` schema/migrations, `static/admin/` admin web UI (vanilla JS, no build step).
- `scripts/container_manager_service.py`, `scripts/inactivity_watcher.py`, `scripts/albert-ai-sandbox-manager.sh`, `scripts/api-key-manager.sh` are thin entry points kept for their historic names.
- `docker/` holds the sandbox desktop image (Dockerfile, startup script, assets). Rebuild the image after any change here (`install.sh` does it automatically when `docker/` changed).
- `nginx/` holds templates installed by `install.sh` / `setup-tls.sh`.
- Runtime state: SQLite at `/opt/albert-ai-sandbox-manager/data/manager.db` (single source of truth), disk images under `/var/lib/albert-sandbox`, nginx routes under `/etc/nginx/albert/sandboxes/`.

## Build, Test, and Development Commands
- `sudo bash install.sh` installs or updates the manager (idempotent; running sandboxes keep running).
- `python -m pytest` runs the test suite (fakes for Docker and disks; no root needed). Install `requirements.txt` and `requirements-dev.txt` into a venv first.
- `albert-ai-sandbox-manager selfcheck` / `reconcile --dry-run` inspect a live installation.

## Coding Style & Naming Conventions
- Python sources follow PEP 8: 4-space indentation, `snake_case` for functions and variables, `CamelCase` for classes, and concise docstrings for public entry points. Keep Python 3.9+ compatible.
- API payloads use lowerCamelCase keys (`autoStart`, `memoryMb`); keep schema additions backwards compatible — clients (ALBERT | AI) depend on the existing field names.
- Database changes go through `db.py` (`init_db` must stay idempotent; bump `SCHEMA_VERSION` and add migration steps for existing installations).
- Every operation that creates host resources must be covered by `reconcile.py` so leftovers are cleaned up.
- Sandboxes must never run outside the isolated network or without the firewall rules: call `netctl.ensure_ready()` before creating or starting containers, and keep `sandboxes.HARDENING_ARGS` for every `docker run`.
- `docker/Dockerfile` pins versions via `ARG`s; never reintroduce `@latest` or runtime `npx` downloads.

## Testing Guidelines
- New tests live in `tests/test_<feature>.py` and use the fixtures in `tests/conftest.py` (`env`, `make_client`).
- Integration tests on a real host must clean up the sandboxes they create (`albert-ai-sandbox-manager remove <name>`).

## Commit & Pull Request Guidelines
- Write imperative, present-tense commit subjects under ~60 characters (e.g., `Stop and remove Albert containers during uninstall`).
- Squash unrelated work; include explanatory bodies when touching install/uninstall scripts, database schema, or Docker build logic.
- Pull requests should describe the change, list manual validation steps (commands run, environments touched), and link issues or tickets.

## Security & Configuration Tips
- Do not commit plaintext API keys, admin passwords or `.env` files.
- Configuration lives in `/opt/albert-ai-sandbox-manager/config/albert.env`; document new settings in README (Configuration table).
- Review Dockerfile edits for additional network exposure and ensure new binaries are sourced from trusted repositories.
