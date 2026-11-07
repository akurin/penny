# Penny on k3s

Penny runs as a single pod on a dedicated node joined to the k3s cluster: GitHub
Actions builds the image, opens an SSH tunnel to the API and applies the
manifests. There is no Service or Ingress — Telegram long polling needs no HTTP
entry, so cert-manager and Traefik are not involved.

## 1. Join and mark the node

On the server, read the join token:

```sh
sudo cat /var/lib/rancher/k3s/server/node-token
```

On the node:

```sh
curl -sfL https://get.k3s.io | \
  K3S_URL=https://<server>:6443 K3S_TOKEN=<node-token> sh -
```

Then label it; an optional taint reserves it for Penny:

```sh
kubectl get nodes -o wide
kubectl label node <node> homelab.morjoff.com/workload=home
# optional: keep other workloads off the node
kubectl taint node <node> dedicated=home:NoSchedule
```

**Networking caveat.** The agent connects outbound to the server, but k3s's
default flannel overlay needs UDP 8472 between nodes. A NAT'd node can join and
still be `Ready`, but cross-node pod traffic may fail. Penny uses the normal pod
network: it opens no ports and only needs outbound access to Telegram, the model API
and the proxy, so it must stay pinned to the node that can reach those endpoints.
Its DNS is pinned to the LAN resolver because the node's `/etc/resolv.conf`
points at systemd-resolved's `127.0.0.53` stub. If the node never becomes
`Ready`, put the nodes on WireGuard/Tailscale or run it as its own single-node
cluster instead.

## 2. GitHub Actions deploy

`.github/workflows/deploy.yml` runs after a successful `Tests` workflow on a
push to `master` (or manually via `workflow_dispatch`, which runs its own checks):
build `deploy/Dockerfile`, push `ghcr.io/<owner>/penny:<sha>` to GHCR, then over
an SSH tunnel create the namespace and both Kubernetes Secrets, apply the
manifests and wait for the rollout.

Configure these under **Settings → Environments → production** in GitHub:

| Secret | Purpose |
|---|---|
| `KUBECONFIG` | points at `https://127.0.0.1:6443`; the tunnel maps that port to the k3s API |
| `CI_DEPLOY_SSH_KEY` | key for the `ci-deploy` SSH account |
| `K3S_HOST` | SSH host reachable from GitHub |
| `TELEGRAM_BOT_TOKEN` | bot token |
| `MODEL_API_KEY` | model key |
| `PENNY_STORES_CONFIG_JSON` | entire contents of the local `config/stores.local.json` file, pasted as one multiline secret |

| Environment variable | Purpose |
|---|---|
| `ALLOWED_USER_IDS` | required; comma-separated numeric IDs |
| `MODEL_NAME` | optional; defaults to `deepseek-flash` |
| `MODEL_BASE_URL` | optional; defaults to `https://api.deepseek.com`; API root without `/chat/completions` (for OpenRouter use `https://openrouter.ai/api/v1`) |
| `STORE_PROXY` | required; credential-free SOCKS5 proxy for store requests, reachable **from the node** (`socks5h://` keeps DNS at the proxy) |
| `TELEGRAM_PROXY` | optional; SOCKS5 proxy for Bot API calls, when `api.telegram.org` is filtered from the node |
| `PENNY_LOG_LEVEL` | optional; defaults to `INFO` |
| `PENNY_LOG_CONTENT` | optional; defaults to `0`; `1` logs private shopping conversations |
| `PENNY_NODE_SELECTOR` | required; `key=value` node label that selects the node, e.g. `homelab.morjoff.com/workload=home` |
| `PENNY_TOLERATION` | optional; `key=value:effect` toleration for a tainted (reserved) node, e.g. `dedicated=home:NoSchedule` |

Use **Environment secrets** for the first table and **Environment variables**
for the second. If the proxy URL ever contains a username or password, move
`STORE_PROXY` back to an Environment secret and update the workflow reference.
The workflow validates the JSON and creates/updates
`penny-stores` from it. Keep the file out of Git; the pod mounts the resulting
Secret at `/app/config/stores.local.json`. The workflow also creates/updates
`penny-env` for the environment variables. The pod manifest no longer overrides
the configured log level or content setting.

The GHCR package must be pullable by the node. The image contains no secrets, so
making the package public is the simplest option; otherwise create a pull secret
and uncomment `imagePullSecrets` in the manifest:

```sh
kubectl -n penny create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<PAT>
```

Do **not** set `PENNY_BROWSER_HEADLESS`: the code needs 5ka in plain headless
and Lenta in the virtual/WebGL mode.

For a manual deploy without GitHub Actions, the commands below still create
the same Kubernetes Secrets from local files.

## 3. Manual fallback

Without CI, build on the target machine (so the Camoufox download matches its
architecture) and import the image into k3s's containerd:

```sh
docker build -t penny:latest -f deploy/Dockerfile .
docker save penny:latest | sudo k3s ctr images import -
sudo k3s ctr images ls | grep penny
```

Then create the secrets from a container-specific env file and the local branch
config, and apply:

```sh
kubectl create namespace penny
kubectl -n penny create secret generic penny-env --from-env-file=.env
kubectl -n penny create secret generic penny-stores \
  --from-file=stores.local.json=config/stores.local.json
kubectl apply -f deploy/k8s/penny.yaml
kubectl -n penny rollout status deployment/penny
```

`deploy/k8s/secret.example.yaml` lists the expected env keys;
`config/stores.example.json` shows the branch config shape.

## 4. First start: history

The bot starts with empty volumes and needs no session files: Магнит and
М.Косметик call the webgate anonymously, Пятёрочка saves its own clearance to
`/app/state/5ka.json`, and Дикси is unblocked from the chat with `/dixy_captcha`.

Carry over the price history if you have one:

```sh
POD=$(kubectl -n penny get pod -l app=penny -o jsonpath='{.items[0].metadata.name}')
kubectl cp data/penny.sqlite3 penny/$POD:/app/data/penny.sqlite3
kubectl -n penny rollout restart deployment/penny
```

Keep exactly **one replica** — Telegram long polling allows a single poller.

## 5. Exercise the bot without Telegram

Agents (and you) can run one request through the real pipeline — intent
parsing, the store round, the selection model and rendering — without sending
anything to the chat:

```sh
kubectl -n penny exec deploy/penny -- /app/.venv/bin/python -m penny.simulate "хлеб"
```

It prints the parsed intent, per-store candidate counts, the notes that would
appear in the reply, and the rendered answer. The environment is inherited from
the container's own process, so it uses the same proxy, model key and store
config; nothing is written to Telegram or to the price history.

## 6. Node hygiene

A node that suspends kills the bot. Disable sleep and lid handling:

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
# /etc/systemd/logind.conf: HandleLidSwitch=ignore HandleLidSwitchExternalPower=ignore
sudo systemctl restart systemd-logind
```

The `data` and `state` PVCs use `local-path`, so they are tied to this node. If
the node is replaced, copy the PV data before deleting it.

## 7. Updates and backups

CI redeploys on every push. For a manual update, rebuild/import and
`kubectl -n penny rollout restart deployment/penny`. Watch the logs with:

```sh
kubectl -n penny logs -f deployment/penny
```

Back up the database with SQLite's online backup (safe while running) and copy
the file out; `state/` contains session cookies and should be treated as a
secret:

```sh
POD=$(kubectl -n penny get pod -l app=penny -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n penny $POD -- /app/.venv/bin/python -c \
  "import sqlite3; sqlite3.connect('/app/data/penny.sqlite3').backup(sqlite3.connect('/app/data/backup.sqlite3'))"
kubectl cp penny/$POD:/app/data/backup.sqlite3 penny-backup-$(date +%F).sqlite3
```

## Resources

Requests are 500m CPU / 1Gi and limits 2 CPU / 3Gi; two Camoufox instances plus
k3s and the OS fit comfortably in 16 GB. The container runs as root because the
Camoufox cache and local-path volumes are root-owned; add
`runAsUser`/`fsGroup` if you want to harden it.
