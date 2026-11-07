# Penny repository guide

Read `README.md` and `deploy/README.md` first. They own the architecture, store
transports, deployment and private branch-config rules. This file only records
the operational details that are easy to rediscover the hard way.

## Production shape

- One replica only: Telegram long polling allows a single poller.
- The pod runs on the home node `x13`, namespace `penny`, with `data/` and
  `state/` PVCs. There is no Service or Ingress.
- Branch addresses, store codes, aliases and verification tokens live in
  `config/stores.local.json` or the `penny-stores` Secret. Never commit them or
  copy their values into logs, issues or agent notes.
- The production environment currently sets `PENNY_STORE_DEADLINE=60`. Do not
  set `PENNY_BROWSER_HEADLESS`: 5ka needs plain headless, Lenta needs the
  WebGL-capable `virtual` mode.

## Access and testing

- From the OrbStack VM, use a cloud k3s control-plane node as the SSH/kubectl
  jump host. Do not run browsers or other heavy work on the control plane;
  exec into the Penny pod for production-path tests.
- Store traffic uses the pod's `STORE_PROXY`; Telegram uses `TELEGRAM_PROXY`.
  Model calls use `MODEL_PROXY` when set. The store and Telegram proxies are
  SOCKS endpoints on x13 and are not directly reachable from the VM.
  Do not assume `127.0.0.1` or the Mac's LAN is the same network.
- Source IP matters to Qrator, ServicePipe and SmartCaptcha. The VM's direct
  egress can be a Cloudflare/WARP exit, so a failure from the VM says nothing
  about production. Test geo/anti-bot behaviour from inside the pod.
- Exercise the real pipeline without Telegram:
  `kubectl -n penny exec deploy/penny -- /app/.venv/bin/python -m penny.simulate "<query>"`.
  It uses the pod's environment but starts a **new process and browser**, so it
  does not exercise the bot's long-lived browser.
- `python -m penny.diagnostics` checks each configured store from a fresh
  process. Neither helper sends Telegram messages or writes price history.

## Store research rules

- Keep the round bounded. No unbounded retries, browser fallback, paid research
  or arbitrary browsing.
- A hard WAF rejection (`401/403/429`) is not transient. Fail it immediately;
  retrying a rejected gateway call only deepens the block.
- Лента renumbers pickup store ids while catalogue aliases stay stable. Resolve
  the current id through the pickup-store list and cache it for the process.
- Qrator binds its rejection to the long-lived browser fingerprint. If only the
  bot's Lenta browser fails while a fresh browser works, recycle the browser,
  not just the Playwright context. The code now closes the Lenta browser on a
  block/challenge failure; do not try to `kill` Camoufox from `kubectl exec`
  because the container AppArmor profile denies real signals. Restart the pod
  only as a fallback.
- The first successful Lenta clearance after a restart can take ~25 s. Do not
  treat a slow challenge as a dead proxy.

## Verification and deployment

- Run `./scripts/uv run --locked ruff check penny tests` and
  `./scripts/uv run --locked python -m unittest discover -s tests`.
- A push to `master` runs Tests and then Deploy: GitHub Actions builds the
  image and applies the manifests over an SSH tunnel. Never deploy by editing
  the running pod; changes must go through the image.
- Logs are JSON lines: `kubectl -n penny logs -f deployment/penny`. Useful
  events are `lenta_clearance`, `lenta_search_retry`, `lenta_failed`,
  `lenta_browser_reset`, `browser_search`, `research_completed` and `failure`.
- Keep investigation logs out of the repository. Record durable architecture
  and motivation in `README.md` or `deploy/README.md`.
