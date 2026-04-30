# status-monitor

A Linux tray watcher for **GCP**, **GitHub**, and **Anthropic / Claude**.
It reads each provider's public status feed, drops everything outside
the products, repo components, and regions you actually depend on, and
surfaces the result as a row of color-coded indicators in the system
tray, one per source:

- green: everything monitored is operational
- yellow: minor degradation or partial outage
- red: major outage or critical impact

Click any indicator to see exactly which service is impacted, grouped by
project, with deep links to the upstream incident page.

The whole point of the project is **filtering**. Status pages are noisy if
you do not slice them; one click to see "GitHub Actions yellow, GCP DNS red"
is the goal.

## Requirements

- Ubuntu 22.04+ or 24.04 (Debian-derived should work; tested target is 24.04)
- GNOME with AppIndicator support. Ubuntu's GNOME ships this on. Vanilla
  GNOME needs `gnome-shell-extension-appindicator` enabled.
- Python 3.10+
- `gcloud` and `gh` CLIs (the bootstrap script installs both)

## Permissions

The daemon needs **no auth at all** at runtime. It only reads public status
pages. Auth is only used by `setup_cli.py`, once, for discovery, and only
read operations.

If you care about least-privilege, set up scoped credentials before running
`setup_cli.py setup`. If you do not, your existing `gcloud` and `gh` logins
will work fine.

### GCP - exact perms used by setup

| Operation                          | Permission                       |
|------------------------------------|----------------------------------|
| `gcloud services list --enabled`   | `serviceusage.services.list`     |
| `gcloud run services list`         | `run.services.list`              |
| `gcloud sql instances list`        | `cloudsql.instances.list`        |
| `gcloud compute instances list`    | `compute.instances.list`         |
| `gcloud container clusters list`   | `container.clusters.list`        |
| `gcloud storage buckets list`      | `storage.buckets.list`           |

The simplest covering role is project-level **`roles/viewer`** (read-only).
For tighter, create a custom role with just the six permissions above. If
you only ever run `setup_cli.py setup` from a personal account that
already has Owner/Editor on the project, that works too; the daemon will not
use the credential.

### GitHub - fine-grained PAT recipe

Default `gh auth login` requests the classic `repo` scope, which is full
read/write across all your repos. We only need read on a single repo:

1. Visit `https://github.com/settings/personal-access-tokens/new`
2. Resource owner: your user or the org that owns the repo
3. Repository access: **Only select repositories** -> pick the one repo
4. Repository permissions (set to **Read-only** for each):
   - `Metadata` (required, auto-granted)
   - `Actions` (so `setup_cli.py` can detect workflows)
   - `Webhooks` (so it can detect configured hooks)
   - `Pages` (only if the repo uses Pages)
5. Generate and copy the token (starts with `github_pat_`)
6. Hand it to `gh`:

```bash
gh auth logout -h github.com
gh auth login --with-token <<< 'github_pat_...'
```

If a permission is missing, autodetect for that area silently skips and
`setup_cli.py` prints a `note:` line. You can still tick the component
manually in the wizard; the daemon does not need the gh token after setup.

Verify scopes any time with:

```bash
python3 setup_cli.py doctor
gh auth status
```

## Install

```bash
cd ~/portainer/status-monitor
./bootstrap.sh                # installs apt deps, gcloud, gh (idempotent)
gcloud auth login             # if not already; see Permissions above for scope
gh auth login                 # if not already; see Permissions for fine-grained PAT
python3 setup_cli.py setup    # interactive wizard
python3 daemon.py             # foreground smoke test
./install_service.sh          # enable autostart on login
```

The wizard walks you through:

1. **GCP** - enter a project ID. It runs `gcloud services list --enabled`,
   maps the result through `data/gcp_product_map.yaml`, and shows a
   checklist of detected status-page products. Optionally auto-detects
   regions for Cloud Run, Cloud SQL, Compute, GKE, and GCS.
2. **GitHub** - enter `owner/repo`. It probes the repo for
   Actions / Issues / Pages / Webhooks and pre-ticks matching components.
3. **Anthropic** - pick from `Claude API`, `Claude Code`, `claude.ai`,
   `Claude Console`, `Claude Cowork`. Defaults to `Claude API` and
   `Claude Code`.

Configuration is written to `config.yaml` next to the scripts. See
`config.example.yaml` for the shape.

## Daily use

Once installed and the service is running, there is nothing to do. Each
indicator refreshes every 60 seconds. Click one for the dropdown, or
right-click any indicator and choose **Settings...** to tweak runtime
behavior.

### Runtime settings

All live under `settings:` in `config.yaml` and are also exposed in the
in-app **Settings...** dialog:

| Key                       | Default       | Effect                                                                                |
|---------------------------|---------------|---------------------------------------------------------------------------------------|
| `poll_interval_seconds`   | `60`          | How often to poll the status feeds.                                                   |
| `show`                    | `per_source`  | `per_source` shows one tray icon per source; `worst_of` collapses to a single icon.   |
| `indicator_order`         | gcp,github,anthropic | Left-to-right tray order (per_source mode only).                                |
| `notify_on_change`        | `true`        | Desktop notification (via `notify-send`) whenever a source's level changes.           |
| `open_on_critical`        | `false`       | Auto-open the source's status page in the browser on transitions to `critical`.       |
| `min_level`               | `minor`       | Suppress issues below this level (one of `info`, `minor`, `major`, `critical`). At the default, scheduled maintenance and unknown-impact entries are hidden; drop to `info` to see them. |

When the client itself has no internet, the daemon detects it via a fast
TCP probe and pauses polling. Indicators show a neutral gray "offline"
state and resume automatically when connectivity returns; this prevents
flaky Wi-Fi from being misreported as upstream service degradation.

## Managing config

```bash
python3 setup_cli.py list                       # show current config
python3 setup_cli.py add gcp k1flow-prod        # add another GCP project
python3 setup_cli.py add github rmf34/another   # add another repo
python3 setup_cli.py add anthropic              # update Anthropic block
python3 setup_cli.py remove gcp k1flow-prod     # drop one
python3 setup_cli.py refresh                    # re-detect everything
python3 setup_cli.py doctor                     # check tools, auth, files
```

After editing config, restart the daemon to pick up changes:

```bash
systemctl --user restart status-monitor
```

## Architecture

```
+--------------+     +-------------------+     +----------------+
|   setup_cli  | --> |    config.yaml    | <-- |    daemon.py   |
+--------------+     +-------------------+     +----------------+
       |                                              |
       | reads                                        | polls every N seconds
       v                                              v
+--------------+                          +-----------------------+
| gcloud / gh  |                          | status.cloud.google   |
|  (discovery) |                          | githubstatus.com      |
+--------------+                          | status.anthropic.com  |
                                          +-----------------------+
                                                      |
                                                      v
                                              +---------------+
                                              | tray indicator|
                                              +---------------+
```

- `setup_cli.py` runs once per change. It uses `gcloud` and `gh` to discover
  what is real in your project/repo, then writes a static `config.yaml`.
- `daemon.py` only reads `config.yaml` and the public status feeds. It
  never calls `gcloud` or `gh` and needs no auth at runtime.

### Status feeds used

| Source    | Endpoint                                                | Format          |
|-----------|---------------------------------------------------------|-----------------|
| GCP       | `https://status.cloud.google.com/incidents.json`        | Google's own    |
| GitHub    | `https://www.githubstatus.com/api/v2/summary.json`      | Statuspage v2 † |
| Anthropic | `https://status.anthropic.com/api/v2/summary.json`      | Statuspage v2 † |

† **Statuspage v2** refers to the public read-only API exposed by every
page hosted on Atlassian's [Statuspage](https://www.atlassian.com/software/statuspage)
product. The endpoint surface is documented at
<https://developer.statuspage.io/> (see "Status API"). Because the JSON
shape is identical across every customer page, one parser handles all
Statuspage-hosted sources.

### Filtering rules

- **GCP**: an incident counts only if (a) one of its `affected_products`
  matches a service in your config and (b) if you specified regions for
  that service, the incident's `currently_affected_locations` overlaps
  your regions (or is global). Resolved incidents (`end` set) are skipped.
- **GitHub / Anthropic**: a component counts only if it is in your
  `components` list and its current status is not `operational`. Open
  incidents are also surfaced when they touch a watched component.

### Severity mapping

| Source    | Upstream value              | Internal level |
|-----------|-----------------------------|----------------|
| GCP       | severity=low                | minor          |
| GCP       | severity=medium             | major          |
| GCP       | severity=high               | critical       |
| Statuspage| degraded_performance        | minor          |
| Statuspage| partial_outage              | major          |
| Statuspage| major_outage / critical     | critical       |
| Statuspage| under_maintenance           | info           |

Each indicator reflects the worst level across the projects routed to it
(per source in `per_source` mode, or globally in `worst_of` mode).

## Extending the GCP product map

`data/gcp_product_map.yaml` maps `gcloud` API names (the left side of
`gcloud services list --enabled`) to GCP status-page product titles
(the right side, which must match exactly).

To add an API:

1. Find the title in `https://status.cloud.google.com/products.json`.
2. Add a line `your.api.googleapis.com: Exact Status Page Title`.
3. Run `python3 setup_cli.py refresh` to pick it up.

If `setup_cli.py setup` reports many unmapped APIs for your project,
that is the signal to extend the YAML.

## Troubleshooting

**Tray indicators never appear.**
On vanilla GNOME, install and enable AppIndicator:

```bash
sudo apt install gnome-shell-extension-appindicator
gnome-extensions enable ubuntu-appindicators@ubuntu.com
# log out and back in
```

**Daemon exits with `config not found`.**
Run `python3 setup_cli.py setup` first. The daemon refuses to start
without a config to avoid silently watching nothing.

**`gcloud services list` fails.**
You probably need `gcloud auth login`, or the active account does not
have `serviceusage.services.list` on the project. Check with:

```bash
python3 setup_cli.py doctor
```

**Incidents never show.**
First sanity check: select GitHub `Pull Requests` in your config; that
component is degraded fairly often and is a good "wire test". Otherwise:

```bash
journalctl --user -u status-monitor -f
```

**I want a custom poll interval.**
Edit `config.yaml`:

```yaml
settings:
  poll_interval_seconds: 30
```

Then `systemctl --user restart status-monitor`.

## Tests

```bash
./run_tests.sh                # runs the whole suite (forwards args to pytest)
./run_tests.sh -k gcp -v      # filter; same as: python3 -m pytest tests/ -k gcp -v
```

The pure filtering logic lives in `pollers.py` and is exercised against
fixture JSON in `tests/fixtures/`. No network or auth needed to run
tests; the GTK side of `daemon.py` is intentionally not tested.

`tests/test_map.py` validates `data/gcp_product_map.yaml` against a
snapshotted GCP products catalog. If GCP renames or removes a product,
the test fails loudly. Refresh the snapshot with:

```bash
./tests/refresh_snapshot.sh   # re-fetches products.json
```

## Layout

```
~/portainer/status-monitor/
  bootstrap.sh                # one-time installer
  install_service.sh          # systemd --user enable
  run_tests.sh                # invokes pytest against tests/
  setup_cli.py                # interactive wizard, add/remove/list/refresh/doctor
  pollers.py                  # pure status-fetch + filter logic (unit-tested)
  daemon.py                   # GTK tray indicator; imports from pollers
  config.yaml                 # generated by setup_cli (gitignore if you add VCS)
  config.example.yaml         # reference shape
  data/
    gcp_product_map.yaml      # gcloud API -> GCP status product title
  systemd/
    status-monitor.service    # user unit, copied by install_service.sh
  tests/
    conftest.py               # adds project root to sys.path
    test_pollers.py           # poller / filter behavior + severity mappings
    test_map.py               # gcp_product_map.yaml vs catalog snapshot
    test_config.py            # setup_cli upsert behavior
    refresh_snapshot.sh       # re-fetch GCP products catalog
    fixtures/
      gcp_one_resolved.json
      gcp_one_active_us_central1.json
      gcp_global_incident.json
      gcp_products_snapshot.json
      statuspage_all_operational.json
      statuspage_one_degraded.json
      statuspage_one_major.json
      statuspage_resolved_incident.json
      statuspage_open_incident.json
```

## What this is not

- Not a billing or quota monitor.
- Not a metric / log watcher (use Cloud Monitoring or Grafana for that).
- Not a notification aggregator. It only surfaces upstream service outages.
- No auth required at runtime; status pages are public.

## Potentially supported services (future)

The current pollers cover GCP, GitHub, and Anthropic. Two feed shapes the
project already understands, so most additions are config-and-glue:

- **[Statuspage v2](https://developer.statuspage.io/)** (Atlassian's
  product, exposed at `/api/v2/summary.json` on every customer page) —
  same shape as GitHub / Anthropic. Drop-in: new pollers entry plus a
  config block.
- **Custom JSON / RSS** (e.g. GCP `incidents.json`) — small per-source
  parser modeled on `poll_gcp`.

### Tier 1 — universal blast radius

When these go down a developer's day stops *regardless* of stack: deps
inherit the failure, the web becomes unreachable, or incident response
itself breaks. Worth supporting first.

- **AWS** — RSS/JSON at `status.aws.amazon.com`. Custom parser (per-
  region, per-service). Biggest blast radius of any single source —
  even non-AWS apps break via dependencies, CDNs, and package mirrors.
- **Cloudflare** — Statuspage v2. DNS, WAF, Workers, R2 all on one feed;
  if it is degraded a huge fraction of the web is unreachable.
- **Slack** — own API at `status.slack.com/api/v2.0.0/current`. Light
  custom parser. Critical because incident comms collapse when Slack is
  down *at the same time* as another outage.
- **Stripe** — Statuspage v2. Tier 1 only if you have a billing path,
  but in that case Stripe down is revenue-down and indistinguishable
  from the product itself being down.

### Tier 2 — opt in if you use it

These only matter if they are in your specific stack. Pick the one or
two you actually depend on; skip the rest.

| Category              | Services                                                      | Format        |
|-----------------------|---------------------------------------------------------------|---------------|
| AI coding / inference | OpenAI, Cursor                                                | Statuspage v2 |
| Hosting / deploys     | Vercel, Fly.io, Render                                        | Statuspage v2 |
| Source / CI           | GitLab, CircleCI                                              | Statuspage v2 |
| Auth                  | Auth0, Clerk                                                  | Statuspage v2 |
| Observability / oncall| Datadog, Sentry, PagerDuty                                    | Statuspage v2 |
| Package registries    | npm, PyPI, Docker Hub                                         | Statuspage v2 |
| Collaboration         | Linear, Notion, Zoom                                          | Statuspage v2 |
| Email / SMS           | Twilio, SendGrid, Resend                                      | Statuspage v2 |

## License

MIT - see [LICENSE](LICENSE).

The tray logos in `data/logos/` (GitHub octocat, Anthropic burst, Google Cloud
emblem) are third-party brand assets used for service identification and are
not covered by this project's license; they remain the property of their
respective owners.
