The first release of Illyriad Notifier provides a private, self-hosted notification inbox and ntfy relay.

- Browse notifications by town, type, category, date, read state and delivery status.
- Create ordered filtering rules with previews, topic overrides and priorities.
- Configure connections, polling, retention and friendly names through the settings UI.
- Track delivery attempts and retry failures, with persistent SQLite storage.
- Import initial history without forwarding it; unmatched events stay in the inbox.

Download `compose.release.yaml` and `.env.example`, configure the administrator password, and follow the README. Linux AMD64 and ARM64 container images are published to GHCR.

Polling has a conservative hourly minimum. Delivery is at-least-once, so an ambiguous network failure can cause a duplicate. Each installation supports one administrator and one Illyriad account.
