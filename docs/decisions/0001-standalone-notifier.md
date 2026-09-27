# ADR 0001: Standalone notification inbox and ntfy relay

Status: Accepted — 2026-09-27

## Scope

Provide a self-hosted app with an administrator settings UI, notification inbox,
ordered filtering rules and ntfy delivery in the first release. Each installation
belongs to one Illyriad player and has one administrator. This is a separate
repository and deployable from Quartermaster.

## Decisions

- Python 3.13, FastAPI, server-rendered Jinja templates, local CSS and SQLite.
  One process/container runs both HTTP handling and a background worker. No SPA,
  Redis, external queue, hosted accounts or shared Quartermaster database.
- Synchronous SQLite and HTTP operations run outside the asyncio event loop.
  The worker has a process lock; service operations serialize network/delivery
  transitions. SQLite transactions atomically record imported events and their
  decisions. Rule matching itself has no I/O.
- Collect the complete Recent Notifications feed. Its observed schema has event,
  type, overall type and town IDs, details, and occurrence timestamps. Normalize
  details to plain text. Reject malformed feeds atomically; no partial baseline.
- First successful fetch establishes the baseline, including an empty feed, and
  never forwards historical events. Subsequent unseen IDs are matched against
  enabled rules, ordered by position then rule ID. OR within selections, AND
  between fields. First match wins; default action keeps the event in the inbox.
- Store each decision, topic and priority at import time. Later rule edits do not
  resend history or alter queued deliveries. Server URL/token changes apply to
  queued messages. Pausing delivery keeps the queue; resuming drains it.
- Keep durable ID tombstones after message retention. This prevents replay when
  the upstream feed repeats events after their local bodies have expired.
- Five delivery attempts with bounded exponential delay; exhausted events require
  an explicit retry. Delivery is at-least-once: an accepted request followed by a
  timeout or a process crash before local commit may be sent again. A `sent` status
  means accepted by ntfy, not proof that a phone received it.
- Conservative hourly minimum for every fetch, including manual tests and failures.
  Persist next eligible time before issuing the request. This is an application
  policy, not a claim about verified upstream limits. Incremental API semantics
  and upstream history windows remain unverified. Long outages can miss events
  that Illyriad has already removed from its feed.
- Admin password is hashed with Argon2. Signed, HttpOnly, SameSite=Strict sessions
  expire after 12 hours and are revoked on password change. All POSTs, including
  login, validate CSRF. Login has a persistent global limiter.
- Encrypt recoverable API keys and ntfy tokens with Fernet. Encryption and cookie
  keys live in a restricted file beside the database: back up the whole volume.
  This protects a database-only copy, not a compromised host or whole-volume copy.
- Use a fixed Illyriad API origin and no redirects. An authenticated administrator
  can configure any HTTP(S) ntfy origin, including private homelab addresses.
  Transport error details and full feed XML must never be logged. No real data in
  demo fixtures. Enabling a real integration is an operator step.

## Deferred

Multiple accounts/users, hosted service operation, digests, additional destinations,
full HTML messages, automatic labeling from unrelated world-data feeds, and faster
polling pending upstream verification.
