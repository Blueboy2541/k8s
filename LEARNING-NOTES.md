# Learning Notes — 2026-07-31

Session notes from building out `fang-stock-metrics` and its monitoring stack.
Organised by subject rather than chronologically, for review.

---

## 1. Python

### Environment variables

```python
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
```

- `.get(key, default)` — never raises. Use when "missing" is a normal case.
- `os.environ[key]` — raises `KeyError`. Use when missing means misconfigured, so
  the app **fails fast at startup** instead of running silently broken.
- Everything from `os.environ` is a **string**, hence the `int(...)` wrapper.

### Type hints

- `-> None`, `list[str]`, `dict[str, float]` are **documentation only**; Python does
  not enforce them at runtime. Type checkers (`mypy`, `pyright`) do, statically.
- `list[str] | None` (PEP 604) is evaluated at runtime and **needs Python 3.10+**.
  On 3.9 it raises `TypeError: unsupported operand type(s) for |`.
- Fix: `from __future__ import annotations` as the **first statement** in the file.
  All annotations become lazily-evaluated strings, so modern syntax works on 3.7+.
- Caveat: libraries that *read* annotations at runtime (`dataclasses`, `pydantic`,
  FastAPI) need real objects and must resolve them via `typing.get_type_hints()`.

### Logging

```python
log.info("ticker=%s price=%.2f", ticker, price)   # good
log.info(f"ticker={ticker}")                      # avoid
```

`%s` placeholders defer string formatting until the message is actually emitted.
An f-string always formats, even if the log level would discard it.

`log.exception(...)` inside an `except` block automatically attaches the traceback.

### Broad `except` is sometimes right

`poll_once()` wraps each ticker in `try/except Exception`. Normally discouraged, but
correct here: one ticker failing must not kill the loop or block the other tickers.
Note it's **inside** the `for`, not around it.

### Threading and shared state

```python
_state_lock = threading.RLock()
_tickers: list[str] = []
```

- Needed because the Flask thread **writes** while the polling loop **reads**.
- **The GIL does not make this safe.** It guarantees single bytecode operations are
  atomic, not multi-line sequences. A `if x in list: list.append(x)` check-then-act
  can be interrupted between the check and the act.
- `with lock:` releases even if an exception is raised. Manual acquire/release can
  deadlock forever on an error path.
- **Keep locked blocks tiny; never do I/O or network calls while holding a lock.**
- `get_tickers()` returns a **copy** so the poll loop can iterate for 10s per HTTP
  request without holding anything.
- `RLock` vs `Lock`: reentrant, so the same thread can acquire twice. Not strictly
  needed today — it's insurance against a future refactor causing self-deadlock.

### Atomic file writes

```python
tmp_path.write_text(...)
os.replace(tmp_path, TICKERS_FILE)
```

`os.replace` is atomic at the filesystem level. A crash mid-write leaves either the
old complete file or the new complete file — never a truncated one.

### Misc

- `global x` is only needed to **rebind** a module-level name. `.append()` / `.remove()`
  mutate in place and don't need it.
- Leading underscore (`_tickers`) = "module-internal". Convention, not enforced.
- `if __name__ == "__main__":` — `__name__` is `"__main__"` only when run directly,
  so importing the module doesn't start the infinite loop.

---

## 2. HTTP servers, WSGI, and Flask

### Raw `http.server`

`BaseHTTPRequestHandler` dispatches by method name: it looks for `do_GET`, `do_POST`,
etc. Undefined verbs automatically return `501`. No routing — `self.path` is ignored
unless you inspect it yourself.

Response order is dictated by the HTTP protocol:
`send_response()` → `send_header()` → `end_headers()` → `wfile.write(bytes)`.
Skipping `end_headers()` makes the client hang forever.

`HTTPServer((host, port), HandlerClass)` — you pass the **class**, and a fresh handler
instance is created per request. That's why handlers hold no state.

`0.0.0.0` vs `127.0.0.1`: binding to loopback inside a container makes it unreachable
from outside, even with the port published. Common bug.

### WSGI

A **calling convention** (PEP 3333), not a library. A WSGI app is any callable:

```python
def app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello"]
```

- A Flask app object **is** a WSGI application (it implements `__call__`).
- Flask's built-in server is for development: no request timeouts, no hardening, limited
  concurrency. Production servers (`waitress`, `gunicorn`, `uWSGI`) implement the same
  interface, so swapping is one line.
- **ASGI** is the async successor (FastAPI/Starlette + `uvicorn`), and also covers
  WebSockets, which WSGI cannot express.
- `prometheus_client.start_http_server()` is **not** WSGI — it uses raw `http.server`.

### Flask vs Laravel

Laravel ≈ **Django**, not Flask. Flask is a microframework (routing + templating +
sessions); no ORM, migrations, auth, or CLI scaffolding — those are separate packages.

**The difference that matters:** classic PHP is request-per-process — globals don't
survive between requests. Flask runs in a **long-lived process**, so module-level state
persists. That's why the ticker list can be an in-memory list, and also why it needs a
mutex. (Laravel Octane has the same property, and the same class of bug.)

---

## 3. prometheus_client

- `Gauge(...)` **self-registers** into a global `REGISTRY` at construction time. There
  is no explicit "register with the server" call.
- `.labels(ticker="META")` lazily creates a **child** metric keyed by label values.
- `start_http_server(port)` runs a background thread serving `generate_latest(REGISTRY)`.
  Nothing is ever pushed to it — every scrape reads current in-memory values.
- `metric.remove(labelvalue)` deletes a labelled series, otherwise a removed ticker keeps
  exporting a stale value forever. Raises `KeyError` if never set.

---

## 4. Docker

### Build context

- `context: ./fang-stock-metrics` defines the set of files sent to the builder.
- Convention: the builder looks for `Dockerfile` at the **context root** (overridable).
- `COPY` paths resolve **relative to the context root**.
- **`COPY` cannot escape the context** — `COPY ../shared/x.py` fails even if the file
  exists on disk.
- Nothing auto-detects `requirements.txt`. The Dockerfile names it explicitly.
- Everything in the context is uploaded, even files never `COPY`'d. Trim with
  `.dockerignore`.

### Layer cache ordering

```dockerfile
COPY requirements.txt .
RUN pip install -r requirements.txt   # cached unless requirements.txt changes
COPY app.py .                          # changes often, so it comes last
```

This ordering is why editing `app.py` doesn't reinstall Flask.

---

## 5. GitHub Actions

### Workflow anatomy

- Workflows **must** live at `<repo-root>/.github/workflows/`. Subdirectories are never
  scanned — this is why the file can't sit inside the app folder.
- `paths:` filter avoids rebuilding on unrelated commits in a monorepo.
- `workflow_dispatch: {}` adds a manual "Run workflow" button — useful for re-triggering
  without a dummy commit.
- `runs-on: ubuntu-latest` = a fresh, ephemeral VM. Nothing persists between runs.
- `id: meta` + `${{ steps.meta.outputs.tags }}` is how data flows between steps.
- YAML quirk: bare `on` parses as boolean `true` in YAML 1.1 (same family as the
  "Norway problem" where `NO` → `false`).

### GITHUB_TOKEN

- **You never create it.** GitHub mints a fresh, repo-scoped, short-lived token per run.
- Your credentials are used exactly once — at `git push`, via your SSH key. Everything
  after that runs on GitHub's own infrastructure under its own authority.
- Identity is the `github-actions[bot]` GitHub App installation, not your account.
- Fork PRs get a **read-only** token regardless of what the YAML requests — the token
  reflects the trust level of the trigger. Authorization is effectively decided at
  `git push` time, so **protecting a branch is protecting your registry.**
- A real secret *is* needed for: other registries (Docker Hub), other repos, or when a
  workflow must trigger another workflow.

### The 403 we hit

```
failed to push ...: 403 Forbidden
```

- The build succeeded; only the push failed.
- **`docker login` succeeding proves nothing about write access.** A read-only token
  authenticates fine and is rejected at the first blob write.
- Fix: Settings → Actions → General → **Workflow permissions** → *Read and write*.
  The `permissions:` block in the YAML cannot elevate above the repo-level cap.
- Other cause worth checking: an orphaned package not linked to the repo
  (Package settings → Manage Actions access).

### Registry choice

Moved from `ttl.sh` (anonymous, tags expire after 24h) to **GHCR** — free, permanent,
and authenticated by the built-in token. GHCR requires a **lowercase** image path.

Tags produced per build: `latest` (moving) and `sha-<commit>` (immutable). The SHA tag
is what makes rollbacks meaningful.

---

## 6. Kubernetes — storage

### The model

- **PVC** = a request ("I need 64Mi, RWO").
- **PV** = actual storage.
- **Dynamic provisioning**: a StorageClass watches for pending PVCs and creates PVs.
- **Static provisioning**: you write the PV by hand.

**The scheduler refuses to place a pod whose volume doesn't exist**, which produces:

```
0/4 nodes are available: pod has unbound immediate PersistentVolumeClaims
```

That node count is misleading — it was never a node problem.

### `storageClassName` semantics

| Value | Meaning |
|---|---|
| omitted | admission controller fills in the cluster default **at creation time only** |
| `""` | explicitly opt out of dynamic provisioning |
| `manual` | binds only to PVs with the same string |

`manual` isn't a keyword — it's an arbitrary label. It works because **no StorageClass
object by that name exists**, so no provisioner acts and only static binding can happen.

Adding a default StorageClass later does **not** fix an already-Pending PVC — it must be
deleted and recreated.

Binding requires class **and** access mode **and** capacity to match. When a PVC won't
bind, diff it field-by-field against the PV.

### `hostPath` vs `local`

| | `hostPath` | `local` |
|---|---|---|
| Scheduler knows where data is | No | Yes, via `nodeAffinity` |
| Usable inline in a Pod spec | Yes | **No** — PV only |
| Creates missing directories | Yes | No (fails the mount) |
| Raw block devices | No | Yes |
| Allowed under restricted Pod Security | **No** | Yes |

**The security difference is the real one.** `hostPath` lets a pod mount any host path
(`/etc`, `/root/.ssh`, `/`) — a direct path from "can create a pod" to "owns the node".
Pod Security Standards ban it at `baseline` and `restricted`. `local` splits the roles:
only an admin creates the PV and chooses the path; users only write claims.

`hostPath` is still correct for pods that legitimately need the host: node-exporter
reading `/proc` and `/sys`, log collectors, CNI plugins.

**Neither enforces capacity.** `storage: 10Gi` is used only for matching. Prometheus is
bounded by `--storage.tsdb.retention.size`, not by the PV.

### `local` volumes pin pods

`nodeAffinity` on the PV is a **hard scheduling constraint** — the pod can only run where
its data physically is. Consequences:

- Drain that node → the workload sits `Pending`, it does **not** relocate.
- Node dies → data dies with the disk.
- This is a feature: `hostPath` would instead land on another node, silently mount a
  **new empty directory**, and reset state with no error at all.

`nodeAffinity` is **immutable** — it asserts a physical fact. Repointing requires
delete + recreate (scale down → delete PVC → delete PV → re-apply). The PVC must go
first because a **finalizer** blocks deletion while a pod references it.

### Reading scheduler errors

```
0/4 nodes are available: 1 node(s) had untolerated taint(s),
                         3 node(s) didn't match PersistentVolume's node affinity
```

Each clause explains a **different** subset of nodes. Here: 3 workers failed affinity,
1 control-plane failed the taint → therefore the PV was pointing at the control plane.
Two hard constraints intersecting to the empty set.

### Taints and tolerations

The inverse of affinity. Affinity = pod says "I want that node." Taint = node says
"stay away unless you tolerate me."

`kubeadm` taints the control plane `node-role.kubernetes.io/control-plane:NoSchedule` so
workloads don't compete with etcd and the API server.

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
```

### Filesystem permissions

`prom/prometheus` runs as uid 65534 (`nobody`); a fresh volume is root-owned →
crash-loop. Fix with `securityContext.fsGroup: 65534`, which makes the kubelet chown the
mount. For `local` volumes the directory must also be created **and chowned** on the node.

### `emptyDir` is not storage

Lives and dies with the pod. Prometheus was using it, so **every restart wiped all
metrics history** — much worse than the 15-day default retention.

---

## 7. Kubernetes — workloads

### Triggering a new image

`kubectl apply` does **nothing** when only the image *content* changed: the spec still
says `:latest`, so the API server sees no diff. Kubernetes tracks the image *reference*,
not the image *content*.

```bash
kubectl rollout restart deployment/x
```

Patches the pod template with a `kubectl.kubernetes.io/restartedAt` annotation → template
hash changes → new ReplicaSet → new pod → `imagePullPolicy: Always` re-pulls.

Better: `kubectl set image ... :sha-<commit>` — a real spec change, and you know exactly
what's running. With `:latest`, `rollout undo` can't actually restore the old image
because both revisions say the same thing.

**Gotcha:** `kubectl delete pod` also re-pulls, so it silently deploys whatever landed on
`main` since. It's an undocumented rollout.

Confirm what's really running:

```bash
kubectl get pod -l app=x -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'
```

### `Recreate` vs `RollingUpdate`

`Recreate` terminates the old pod before starting the new one. Required here because a
`ReadWriteOnce` volume can't be mounted by two pods across nodes — a rolling update
would deadlock.

RWO means one **node**, not one pod (since 1.22 multiple pods on the same node can share).

### `kubectl apply` internals

Not an overwrite — a **three-way merge** between what you submit, what's live, and the
stored last-applied annotation. That's how it knows to delete a field you removed while
leaving cluster-managed fields (`status`, assigned `clusterIP`) alone. Idempotent.

Some fields are **immutable** (PVC size/class, Service `clusterIP`, PV `nodeAffinity`) and
error rather than no-op.

Don't `kubectl apply -f .` in a folder containing a Job you don't want to re-run.

### Object lifecycles

| Command | Pod | Data |
|---|---|---|
| `delete pod` | recreated in seconds | survives |
| `delete deployment` | gone, stays gone | survives |
| `delete pvc` | runs until restart | **destroyed** |

`persistentVolumeReclaimPolicy: Retain` means the on-disk directory survives even PV/PVC
deletion — you can point a fresh PV at the same path.

---

## 8. Prometheus

### Pull model

Prometheus **scrapes**; nothing is pushed. Every scrape reads whatever is in memory at
that instant.

### The unstable `instance` label

Kubernetes SD derives `instance` from the pod IP, so **every pod restart creates a brand
new time series** and graphs show duplicated/broken lines.

Fixes:

```promql
max by (ticker) (fang_stock_price_usd)     # query-time; also fixes historical data
```

```yaml
relabel_configs:
  - target_label: instance
    replacement: fang-stock-metrics        # scrape-time; only affects future samples
```

Do both — relabel so you stop generating garbage, aggregate so past data is usable too.

### `role: endpoints` discovers every port

A Service with two ports yields **two targets**. The admin UI on 8080 has no `/metrics`,
so it becomes a permanently failing target. Filter it:

```yaml
  - source_labels: [__meta_kubernetes_endpoint_port_name]
    regex: metrics
    action: keep
```

### Staleness lookback

Prometheus returns the last sample within **5 minutes** for instant queries. A 20-second
restart gap is therefore invisible in dashboards and alerts — it only shows in raw data
or short-window `rate()`. Worth knowing before optimising a gap that doesn't matter.

### Retention

```
--storage.tsdb.retention.time=30d
--storage.tsdb.retention.size=8GB
```

Whichever hits first wins. The **size** cap is what actually protects the volume, since
PV capacity enforces nothing.

### `--web.enable-lifecycle`

Without it there's no `/-/reload` and config changes need a pod restart (which, on
`emptyDir`, destroyed all data). With it:

```bash
curl -X POST http://NODE_IP:30090/-/reload
```

### ServiceMonitor

A **CRD from the Prometheus Operator** — not core Kubernetes. Check with:

```bash
kubectl get crd servicemonitors.monitoring.coreos.com
```

We don't have the Operator, so scrape config lives in the `prometheus-config` ConfigMap.

If you ever adopt it, the selector chain breaks silently in two classic places:
`endpoints[].port` is the port **name** (not number), and `kube-prometheus-stack` sets
`serviceMonitorSelector` to require its Helm release label.

---

## 9. Remote access

```bash
ssh -N -L 8080:localhost:30801 user@node   # single service; middle host resolves REMOTELY
ssh -N -D 1080 user@bastion                # SOCKS proxy for the whole network
```

For kubectl through a bastion, **SOCKS is cleaner than port-forwarding** because TLS still
validates — you connect to the API server's real address, only the route changes:

```yaml
cluster:
  server: https://NODE_IP:6443
  proxy-url: socks5://127.0.0.1:1080
```

With `-L` you'd hit `x509: certificate is valid for 10.x.x.x, not 127.0.0.1` and need
`tls-server-name: NODE_IP`. Never "fix" that with `insecure-skip-tls-verify`.

`~/.ssh/config` supports `ProxyJump` and `LocalForward` to make this one short command.

`kubectl port-forward svc/x 8080:8080` is the SSH-free alternative — targets the Service
by name, but dies when the pod restarts.

---

## 10. Architecture discussions (no code written)

### PyTorch on this data

- **Avoid price prediction.** An LSTM converges to "tomorrow ≈ today" — it looks great
  by RMSE and is worthless. Always compare against the naive baseline.
- **Better targets:** anomaly detection (emit the reconstruction error as a gauge and
  alert on it) or volatility, which genuinely clusters.
- **The real value is the deployment loop:** training as a CronJob, model artifacts on a
  volume, hot-reload on change, instrumenting inference with Prometheus.
- Constraints here: Finnhub `/quote` has no history (Prometheus *is* the history, and
  retention caps it), ~390 points/ticker/day is tiny, and the torch CPU wheel would OOM a
  128Mi pod. Train offline, serve with NumPy or ONNX.

### Should the AI part be a separate service?

Yes — but split on **resource profile and failure blast radius**, not on "it's AI".
Price collection is core; anomaly scoring is optional and must not be able to kill it.

Three workloads: exporter (unchanged), training CronJob, scoring service. Training and
scoring share **one image with two entrypoints**.

The elegant part: **Prometheus is already the integration layer.** The scorer queries
Prometheus for history and exposes its own metrics — no service-to-service calls, no
shared DB. It can even discover tickers from label values instead of reading the file.

Watch out: RWO can't be shared across nodes, so a shared model volume needs RWX.

### Kafka?

Not at this scale. 6 tickers × 1/min = **0.1 msg/sec**; a broker idles at 1–2 GB, ~10–20×
the whole application.

Bigger issue is the **impedance mismatch**: Kafka is push, Prometheus is pull, so Kafka
doesn't replace anything — it inserts a layer *before* the exporter you still need.

The legitimate trigger is switching to the **Finnhub WebSocket trade stream** — thousands
of msg/sec creates the actual problem. Change the data source first; the infrastructure
follows. For the concepts at homelab scale, Redis Streams or NATS JetStream give consumer
groups and offsets at ~50Mi.

---

## What we changed today

- Added a Flask admin UI to `app.py` for adding/removing tickers, persisted to JSON.
- Added `from __future__ import annotations` for Python 3.9 compatibility.
- Pinned `Flask==3.0.3` so local and container match.
- Replaced in-cluster Kaniko builds with **GitHub Actions → GHCR**
  (`.github/workflows/fang-stock-metrics.yml`), tagging `latest` + `sha-<commit>`.
- Added `pvc.yaml` + `pv.yaml` (static, `local`, pinned to **node02**).
- Prometheus: `emptyDir` → PVC/PV on **node03**, `fsGroup: 65534`, `strategy: Recreate`,
  retention time+size, `--web.enable-lifecycle`, probes, image pinned to `v3.13.2`.
- Prometheus scrape config: filtered to the `metrics` port, pinned the `instance` label.

## Still open

- [ ] Swap Flask's dev server for `waitress` (production WSGI warning).
- [ ] `StorageClass` named `manual` with `volumeBindingMode: WaitForFirstConsumer` —
      would have prevented the control-plane binding trap entirely.
- [ ] `.dockerignore` to keep manifests out of the build context.
- [ ] Lower probe `initialDelaySeconds` to shrink the restart gap.
- [ ] `kaniko-build-job.yaml` is now redundant.
- [ ] Argo CD for actual CD (GitHub runners can't reach the homelab; deploys are manual).
- [ ] Longhorn if node-pinning becomes annoying, or when the ML service needs RWX.
