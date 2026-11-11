"""ALBERT | AI Sandbox manager core.

The package holds the whole sandbox lifecycle (create, start, stop, remove),
resource limits and per-client quotas, the reconcile/cleanup logic, the
inactivity watcher, the REST API (client + admin) and the CLI. SQLite is the
single source of truth; Docker, nginx and the per-sandbox disk images are
derived state that the reconcile step keeps in sync.
"""

__version__ = "2.0.0"
