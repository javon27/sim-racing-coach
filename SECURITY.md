# Security policy

## Reporting a vulnerability
Please report it privately via
[GitHub Security Advisories](https://github.com/javon27/sim-racing-coach/security/advisories/new),
not in a public issue. I'll reply as soon as I can; this is a one-person project.

## What's in scope
- **The localhost sidecar** (`simcoach/server`, `127.0.0.1:8765`). It must only be
  drivable by the Nestor app. `simcoach/server/security.py` enforces a Host allowlist
  (against DNS rebinding) and an exact Origin allowlist (against CSRF and cross-site
  WebSockets). Any way for a web page or another local app to reach a guarded route
  is a vulnerability.
- **Your Anthropic API key.** It is stored in the OS keyring and should never be
  written to disk, logged, or sent anywhere except the Anthropic API.
- **Game access.** Nestor only opens the plugin-published shared-memory buffers
  read-only and reads LMU's local REST API and result logs. It must never open or read
  the game's process memory.

Out of scope: the cost of your own API usage, and anything that requires an attacker
who already controls your Windows account.
