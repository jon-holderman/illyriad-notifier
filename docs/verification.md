# Release verification — 2026-09-27

## Verified

- Python 3.13: locked dependency installation succeeds.
- 41 automated tests pass: feed validation, town-independent events, first-match
  rules, baseline import, restart deduplication, retention tombstones, delivery
  retry exhaustion/recovery, encrypted secrets, no redirect following, input
  validation, authenticated pages, CSRF, sign-in throttling and session revocation.
- Ruff lint, Ruff formatting check and Pyright pass.
- Podman Compose builds and runs the non-root container. Its healthcheck succeeds.
- Container recreation preserves the review volume's eight fictional events and
  two saved rules; the administrator session also survives normal recreation.
- Live Illyriad parser verification accepted 1,072 events, 16 types and nine
  categories. Town ID -1 is supported. Only aggregate counts were printed; the
  live API key and notification payloads were not saved in the repository.
- Browser review covers the inbox, settings, rule editor and a type+town preview
  matching one of eight fictional events. Mobile rule/settings pages were checked
  at 390px with no page-level horizontal overflow; tables scroll independently.

## Limits

- ntfy delivery was validated with fake HTTP transports, including failure and
  retry paths. A real ntfy endpoint/token still needs configuring and a test send.
- No real notifications were published. The localhost:8086 review instance has
  fictional data, no Illyriad key, no ntfy connection and forwarding paused.
- Upstream polling limits, incremental fetching and history windows remain
  unverified; the app enforces its conservative hourly minimum.
- Tests emit one dependency deprecation warning: Starlette still supports the
  installed httpx TestClient integration but recommends httpx2 for future use.

## Local review instance

The Compose project is `illyriad-notifier-review`, with port 8086 bound to
localhost and a separate named volume. It is for review only. Start the actual
installation using the README's normal Compose project and a fresh volume.

Stop the review instance without deleting its volume:

```sh
podman compose -p illyriad-notifier-review down
```
