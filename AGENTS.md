# Illyriad Notifier

Read README.md and docs/decisions/0001-standalone-notifier.md before changing behavior.
This is a standalone single-administrator app, independent of Quartermaster.

Use Python 3.13, FastAPI with Jinja templates, and SQLite. Keep feed parsing and
rule matching independent of HTTP routes. Do not add a separate queue, database
server, SPA framework, or multi-user hosting without a documented decision.

Run `uv sync --frozen`, `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pyright` before completion.
Use `podman compose up --build` for container validation when available.

Keep credentials and real notification payloads out of source, fixtures, logs,
and screenshots. External message delivery tests must use a fake transport.
Test CSRF, authentication, rule order, initial import, restart deduplication,
retention, and retries when changing the corresponding behavior.
