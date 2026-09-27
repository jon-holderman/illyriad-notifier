Fix stylesheet loading behind HTTPS reverse proxies. Static asset links now use a relative path, preventing blocked HTTP requests from an HTTPS page.

Includes a regression test for TLS termination at the proxy. No database or settings changes are required.

Linux AMD64 and ARM64 images and the Compose bundle are included.
