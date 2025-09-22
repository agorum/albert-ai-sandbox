# Repository Guidelines

## Project Structure & Module Organization
- `scripts/` contains the host-facing Python services and shell wrappers; `container_manager_service.py` exposes the REST API and `albert-ai-sandbox-manager.sh` orchestrates Docker lifecycle tasks.
- `docker/` holds the sandbox desktop image (Dockerfile, startup script, assets). Rebuild the image after any change here.
- Runtime state lives in `data/` (created on demand) and SQLite is addressed through `MANAGER_DB_PATH`. Keep configs under version control in `config/`.

## Build, Test, and Development Commands
- `bash install.sh` installs the manager into `/opt/albert-ai-sandbox-manager` with required permissions.
- `docker build -t albert-ai-sandbox:latest docker` refreshes the desktop container image.
- `python scripts/container_manager_service.py` starts the Flask REST manager; export `MANAGER_PORT`, `MANAGER_DB_PATH`, or `MANAGER_DATA_DIR` to override defaults.
- `python scripts/api_key_manager.py list` inspects registered API keys; pair with `create`/`revoke` during local testing.

## Coding Style & Naming Conventions
- Python sources follow PEP 8: 4-space indentation, `snake_case` for functions and variables, `CamelCase` for classes, and concise docstrings for public entry points.
- Shell automation is Bash-based. Mirror the defensive patterns in `scripts/common.sh`, prefer functions over inline loops, and log with `[TRACE]`/`json_emit` helpers when extending the manager CLI.
- API payloads use lowerCamelCase keys (`autoStart`, `ownerKeyHash`); keep schema additions backwards compatible.

## Testing Guidelines
- Run the lifecycle smoke test with `python scripts/smoke_test_manager.py --key <PLAINTEXT_KEY>` against a locally running manager.
- New unit tests should live under a future `tests/` package, using `pytest`-style `test_<feature>.py` files that mirror modules in `scripts/`.
- Integration tests must clean up containers they create; rely on the CLI `remove`/`delete` commands to avoid orphaned resources.

## Commit & Pull Request Guidelines
- Write imperative, present-tense commit subjects under ~60 characters (e.g., `Stop and remove Albert containers during uninstall`).
- Squash unrelated work; include explanatory bodies when touching install/uninstall scripts, database schema, or Docker build logic.
- Pull requests should describe the change, list manual validation steps (commands run, environments touched), and link issues or tickets. Attach screenshots or command output when user-facing behaviour shifts.

## Security & Configuration Tips
- Do not commit plaintext API keys or `.env` files; rely on environment variables when exercising the manager locally.
- Update documentation and defaults together when changing ports, image tags, or allowed image policies; keep `MANAGER_ALLOWED_IMAGES` aligned with production expectations.
- Review Dockerfile edits for additional network exposure and ensure new binaries are sourced from trusted repositories.
