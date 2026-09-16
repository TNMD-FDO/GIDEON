---
verified_against:
  - pin: images.grafana
    version: main at 2026-09-03
  - pin: images.prometheus
    version: latest docs (3.14.0) at 2026-09-03
  - pin: images.node-exporter
    version: master at 2026-09-03
  - pin: images.dcgm-exporter
    version: main at 2026-09-03
  - pin: images.postgres-exporter
    version: master at 2026-09-03
  - pin: images.cadvisor
    version: master at 2026-09-03
  - pin: images.blackbox-exporter
    version: master at 2026-09-03
  - pin: images.caddy
    version: 2.11 docs tree at 2026-09-03
---
# Observability stack primary-source check (slice-0 ticket 07)

Scope: Prometheus + Alertmanager + Grafana + exporters (node_exporter, DCGM exporter,
postgres_exporter, cAdvisor, blackbox_exporter) behind Caddy, on a single-host Docker Compose
stack. Every claim below is chased to the upstream doc, README/CHANGELOG, or source that owns it;
Grafana's LDAP/provisioning behavior in particular is confirmed against `grafana/grafana`
source (`main` branch) because the docs pages don't cover it. Research done 2026-09-03.

---

## Findings table

| # | Item | Answer | Version / source |
|---|------|--------|-------------------|
| 1a | `ldap.toml` `$__file{}`/`$__env{}` | Both work. `ldap.toml` is expanded with `setting.ExpandVar`, the **same** expander registry (`env`, `file`) as `grafana.ini` — confirmed in source, not shown in the LDAP docs page's own example. | `grafana/grafana` `main`, `pkg/setting/expanders.go` + `pkg/services/ldap/settings.go` |
| 1b | `GF_SECURITY_ADMIN_PASSWORD__FILE` | Supported; the `__FILE` suffix works for **any** `GF_<Section>_<Key>` env var, not just this one. | grafana.com Configure Docker doc, `latest` |
| 1c | Sub-path + Caddy `handle_path` vs `handle` | `serve_from_sub_path=true` is for when the proxy does **not** strip the prefix (Caddy `handle` + `reverse_proxy`, prefix forwarded as-is). Default `serve_from_sub_path=false` pairs with a proxy that **does** strip the prefix (Caddy `handle_path` + `reverse_proxy`). Both need `root_url` to include `/grafana/`. | grafana.com "Run Grafana behind a reverse proxy" tutorial, `latest` |
| 1d | LDAP group mapping / unmatched user | `org_role`+`grafana_admin` work as documented. A user matching **no** `group_mappings` entry gets `ErrInvalidCredentials` (login refused) **and** `IsDisabled = true` is set on the user record — confirmed in source, undocumented on the docs page. | `grafana/grafana` `main`, `pkg/services/ldap/ldap.go` |
| 1e | `auth.ldap.allow_sign_up` | Default `true`; `false` restricts login to already-existing Grafana users. | grafana.com LDAP doc + `conf/defaults.ini`, `main` |
| 1f | `disable_login_form` | Global switch, no per-user/role scoping in the setting itself — a break-glass local admin still gets the LDAP login flow if the form is hidden. Default `false`. | grafana.com Configure Grafana doc, `latest` |
| 1g | Provisioning datasources/dashboards | Standard `apiVersion: 1` YAML under `/etc/grafana/provisioning/{datasources,dashboards}`; dashboards provider `options.path` + `foldersFromFilesStructure: true` mirrors the on-disk folder tree (requires `folder`/`folderUid` unset, max 4 levels deep). | grafana.com Provisioning doc, `latest` |
| 1h | Disable Grafana's own alerting | `[unified_alerting] enabled = false` (env `GF_UNIFIED_ALERTING_ENABLED=false`) still exists and is still honored in current Grafana; legacy alerting itself was removed in **v11.0.0** so there is no fallback engine — `false` just turns the Alerting UI/API off. | grafana.com Configure Grafana doc + `conf/defaults.ini` (`main`); legacy-removal per Grafana migration/blog docs |
| 1i | Analytics/update-check switches | `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false` — all default `true`, all three are the complete phone-home switches. | `conf/defaults.ini`, `grafana/grafana` `main` |
| — | Grafana OSS current version | **13.2.0** (Docker Hub `grafana/grafana-oss`, `latest` tag) | Docker Hub / grafana.com What's New, checked 2026-09-03 |
| 2 | Alertmanager `email_configs` | All fields exist as documented; `auth_password_file` added by PR #3038 (merged 2022-09-16, first released ~v0.25.0). **`require_tls: false` does NOT opportunistically try STARTTLS** — the STARTTLS block in `notify/email/email.go` is only entered `if *n.conf.RequireTLS`; `false` sends over the raw connection unless the smarthost itself is an implicit-TLS (port-465-style) endpoint. | `prometheus/alertmanager` `main`, `notify/email/email.go`; current release **v0.34.0** |
| 2 | v2 alerts API | `POST /alerts` (i.e. `/api/v2/alerts`) takes a `postableAlerts` array; each alert has `startsAt`, `endsAt`, `annotations` (label set) plus the base `alert` fields (`labels`, `generatorURL`). | `prometheus/alertmanager` `main`, `api/v2/openapi.yaml` |
| 2 | Per-integration metrics | `alertmanager_notifications_total{integration=...}` and `alertmanager_notifications_failed_total{integration=...,reason=...}`, both `CounterVec`s, confirmed in source. | `prometheus/alertmanager` `main`, `notify/metrics.go` |
| 2 | Sub-route `group_wait`/`repeat_interval` | Both (plus `group_interval`) are settable per nested route and inherited from the parent when omitted. | prometheus.io Alerting configuration doc, `latest` |
| 3 | `--storage.tsdb.retention.time` | Default `15d`; accepts `y w d h m s ms`. | prometheus.io Storage doc, `latest` (Prometheus **3.14.0**) |
| 3 | `absent()`/`absent_over_time()` | Both exist as documented, for "metric has vanished" alerts. | prometheus.io Querying/functions doc, `latest` |
| 4 | node_exporter Docker | README's own compose/`docker run` recipe: `--path.rootfs=/host`, `-v "/:/host:ro,rslave"`, `network_mode: host`, `pid: host`. | `prometheus/node_exporter` `master` README |
| 4 | systemd collector | **Disabled by default** (`registerCollector("systemd", defaultDisabled, ...)`); `node_systemd_unit_state` and `node_systemd_timer_last_trigger_seconds` both exist in current source; needs a D-Bus connection (`/run/dbus/system_bus_socket` mount is the community-documented fix — not in the official README). | `prometheus/node_exporter` `master`, `collector/systemd_linux.go` |
| 4 | filesystem collector default exclude | `^/(dev\|proc\|run/credentials/.+\|sys\|var/lib/docker/.+\|var/lib/containers/storage/.+)($\|/)` — does **not** special-case `/host/...`, and does **not** exclude a bind-mounted `/data`; nothing extra is needed to include it. | `prometheus/node_exporter` `master`, `collector/filesystem_linux.go` |
| 5 | DCGM exporter | Current tag **`4.6.0-4.8.3-distroless`** (`nvcr.io/nvidia/k8s/dcgm-exporter`); embeds its own `nv-hostengine` via `libdcgm` by default (no separate host DCGM needed); default counters file `/etc/dcgm-exporter/default-counters.csv`, override with `-f <path>`; `DCGM_FI_DRIVER_VERSION` is a **label field**, not a standalone metric. Blackwell (GB200/GB300) support starts appearing in the 4.4.x DCGM line, expands through 4.6.0. | NVIDIA/dcgm-exporter README (`main`) + `docs.nvidia.com` DCGM changelog/install docs; GitHub Releases API |
| 6 | postgres_exporter custom queries | `--extend.query-path`/`queries.yaml` marked **DEPRECATED since v0.13.0** (2023-06-21) and still present, undocumented removal date, upstream README explicitly recommends `sql_exporter` for generic SQL. `DATA_SOURCE_URI`, `DATA_SOURCE_PASS_FILE` (and `DATA_SOURCE_URI_FILE`) all exist. Current version **v0.20.1**. | `prometheus-community/postgres_exporter` `master` README/CHANGELOG |
| 6 | sql_exporter | Postgres target via a plain DSN string; a `collector` block has `metrics` entries with `type: gauge`, `key_labels`, `query`. Current version **0.24.8**, on Docker Hub (`burningalchemist/sql_exporter`), MIT-licensed. | `burningalchemist/sql_exporter` README/LICENSE, `master` |
| 7 | cAdvisor | Official image moved to **`ghcr.io/google/cadvisor`** as of **v0.53.0**; `gcr.io/cadvisor/cadvisor` is the pre-v0.53.0 path only, and Docker Hub's `google/cadvisor` is stale/deprecated. Official example mounts `/var/run:/var/run:rw` (not `:ro`). `container_last_seen` and `container_memory_working_set_bytes` both confirmed. Current version **v0.60.5**. | `google/cadvisor` `master`, `docs/running.md` + `docs/storage/prometheus.md`; GitHub Releases API |
| 8 | Blackbox exporter | `probe_success`, `probe_ssl_earliest_cert_expiry` confirmed in source; `tls_config.server_name` defaults to the target host (SNI) when unset and can be overridden; the classic `__param_target`/`__address__` relabel chain is in the upstream README verbatim. Current version **v0.28.0**, on both Docker Hub and Quay.io. | `prometheus/blackbox_exporter` `master` README + `prober/http.go` |
| 9 | Caddy `/metrics` | The admin API's `/metrics` (`:2019/metrics`) is on **by default**, no config needed, for Go-runtime/admin metrics; the `metrics` global option/`servers { metrics }` block is what turns on the richer `caddy_http_*` per-route metrics. **No TLS-certificate-expiry metric exists** among Caddy's own metric names — confirmed. `handle_path` implicitly does `uri strip_prefix`; plain `handle` does not. | caddyserver.com `/docs/metrics` and `/docs/caddyfile/directives/handle_path`, Caddy **2.11** docs tree |
| 10 | journald log driver | `docker logs` keeps working (reads back through journald); `journalctl CONTAINER_NAME=<name>` is the documented filter; `tag` sets `CONTAINER_TAG`/`SYSLOG_IDENTIFIER`. | docs.docker.com journald driver doc |
| 11 | Registry endpoints | Quay.io: `cdn.quay.io` + `cdn01–06.quay.io` (ports 80/443), `*.quay.io` wildcard also accepted. NVIDIA: `nvcr.io` plus NVIDIA's own download domains (`developer.download.nvidia.com`, `repo.download.nvidia.com` documented; a distinct nvcr.io blob/CDN host is **not** independently confirmed here — flagged). GCR/Artifact Registry: `gcr.io`, `pkg.dev`, and (for restricted-VIP setups) `storage.googleapis.com`/`199.36.153.4/30`. Docker Hub availability: node_exporter, postgres_exporter, Alertmanager, Prometheus, blackbox_exporter, sql_exporter all ship on Docker Hub too; DCGM exporter and current cAdvisor do **not** (nvcr.io- and ghcr.io-only respectively). | Red Hat KB 7084334 (quay.io CDN list); docs.nvidia.com DGX A100 network-config; Google Cloud Artifact Registry GKE private-clusters doc |
| 12 | Grafana-managed alerting as the sole engine | File provisioning covers rules/contact points/policies/mute timings under `/etc/grafana/provisioning/alerting/`; provisioned resources are **read-only in the UI**. SMTP is fully env/file-configurable (`GF_SMTP_*`, `GF_SMTP_PASSWORD__FILE`); **default `startTLS_policy` is `OpportunisticStartTLS`** (opposite of standalone Alertmanager's default), and there is **no `ca_file`/CA-trust setting** — a private relay CA must go into the container's OS trust store. The contact-point test route (`POST /api/alertmanager/grafana/config/api/v1/receivers/test`) exists in current source and sends a real test notification; there is **no POST route to inject a synthetic alert into Grafana's built-in Alertmanager** (`/api/alertmanager/grafana/api/v2/alerts` is GET-only) — only an external-Alertmanager-datasource proxy gets the POST route. HTTP Basic Auth, when enabled (default), authenticates against **both** the LDAP client and the local Grafana user store in one chain — the local break-glass admin's Basic Auth keeps working with LDAP on. A "dead man's switch" (always-firing `vector(1) > 0` rule) is a documented pattern, but the *watching* half is a Grafana Cloud IRM feature, not something Grafana OSS provides on its own. `noDataState: Alerting` does give an `absent()`-equivalent. | grafana.com Alerting file-provisioning, SMTP config, meta-monitoring, and IRM-heartbeat docs (`latest`); `grafana/grafana` `main` source for the routing table, SMTP TLS client, and Basic Auth wiring |

---

## 1. Grafana OSS

**Current version**: Docker Hub `grafana/grafana-oss:latest` → **13.2.0** (checked 2026-09-03; the
13.x line released April 2026, 13.2.0 in August 2026 per grafana.com's What's New index and
GitHub releases — https://github.com/grafana/grafana/releases, https://grafana.com/docs/grafana/latest/whatsnew/).

### 1a. `ldap.toml` and `$__file{}`/`$__env{}`

The **docs page** (`configure-security/configure-authentication/ldap/`) shows only the `$__env{}`
form in its sample `bind_password = '$__env{LDAP_BIND_PASSWORD}'`, and separately documents
`$__file{/etc/secrets/gf_sql_password}` as a `grafana.ini` example under "Variable expansion"
(`setup-grafana/configure-grafana/#variable-expansion`). Neither docs page states outright that
`ldap.toml` supports `$__file{}` too — so this had to be confirmed in source:

- `pkg/services/ldap/settings.go` reads `ldap.toml` and calls `setting.ExpandVar(string(fileBytes))`
  before TOML-decoding (`github.com/grafana/grafana/pkg/services/ldap/settings.go`, `main`).
- `setting.ExpandVar` (`pkg/setting/expanders.go`) runs the **same** `expanders` list used for
  `grafana.ini`: `env` (priority -10) and `file` (priority -5), matched against the shared regex
  `\$(|__\w+){([^}]+)}`. The `file` expander does `os.ReadFile` + `strings.TrimSpace`. `vault` is
  registered separately by Grafana Enterprise and is not available in OSS.

So `ldap.toml`'s `bind_password = '$__file{/run/secrets/ldap_bind_password}'` is supported by the
same mechanism as `$__env{}` — this is a source-level finding, not shown on the docs page itself.

Source: https://github.com/grafana/grafana/blob/main/pkg/setting/expanders.go,
https://github.com/grafana/grafana/blob/main/pkg/services/ldap/settings.go (both `main`, checked
2026-09-03). Docs: https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-authentication/ldap/,
https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#variable-expansion.

### 1b. `GF_SECURITY_ADMIN_PASSWORD__FILE`

Documented directly: "You can apply this technique to any configuration options in
`conf/grafana.ini` by setting `GF_<SectionName>_<KeyName>__FILE` to the file path that contains
the secret information," with the admin-password case given as the worked example
(`GF_SECURITY_ADMIN_PASSWORD__FILE=/run/secrets/admin_password`).

Source: https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/, `latest`.

### 1c. Sub-path behind Caddy: `handle_path` vs `handle`

The official "Run Grafana behind a reverse proxy" tutorial's **primary** recommended pattern is:
the reverse proxy **strips** the sub-path before proxying (its nginx example does
`location /grafana/ { proxy_pass http://grafana; }` plus `rewrite ^/grafana/(.*) /$1 break;`), and
`root_url` includes `/grafana/` for link generation, while `serve_from_sub_path` is left at its
default `false`.

`serve_from_sub_path = true` is presented as an explicit **alternative**, under the heading
"Alternative for serving Grafana under a sub path": *"You only need this if you don't handle the
sub path serving via your reverse proxy configuration. If you don't want or can't use the reverse
proxy to handle serving Grafana from a sub path, you can set the configuration variable
`serve_from_sub_path` to true."* — i.e. it's for when the proxy forwards the full `/grafana/...`
path unchanged and Grafana itself strips it internally.

Mapped onto Caddy: `handle_path /grafana/* { reverse_proxy ... }` strips the prefix before
proxying → pairs with `serve_from_sub_path=false` (default). Plain `handle /grafana/* { reverse_proxy
... }` forwards `/grafana/...` untouched → pairs with `serve_from_sub_path=true`. Either way
`root_url` must include `/grafana/`. Note: a related GitHub issue (`grafana/grafana#72577`)
documents that this behavior flipped once between Grafana v9 and v10 — worth pinning the Grafana
version if this bites.

Sources: https://grafana.com/tutorials/run-grafana-behind-a-proxy/ (`latest`);
https://caddyserver.com/docs/caddyfile/directives/handle_path (Caddy 2.11 doc tree);
https://github.com/grafana/grafana/issues/72577 (flagged as a secondary/community report of the
v9→v10 behavior change, not itself a spec).

### 1d. LDAP group mapping and unmatched users

`org_role`/`grafana_admin` work exactly as documented (`org_role = "Admin"`, `grafana_admin =
true` in a `[[servers.group_mappings]]` block). What happens to a user who matches **no** mapping
is not stated on the docs page, so this is a source-only finding:

`pkg/services/ldap/ldap.go`, function building the external user (comment verbatim): *"If there
are group org mappings configured, but no matching mappings, the user will not be able to login
and will be disabled"* — sets `extUser.IsDisabled = true`. Separately, `validateGrafanaUser`
returns `ErrInvalidCredentials` (login refused) under the same condition (groups configured, no
`OrgRoles` assigned, not a Grafana admin).

Source: https://github.com/grafana/grafana/blob/main/pkg/services/ldap/ldap.go, `main`, checked
2026-09-03.

### 1e. `auth.ldap.allow_sign_up`

Default `true` ("Grafana to create users on successful LDAP authentication"); `false` restricts
login to pre-existing Grafana users. Confirmed against `conf/defaults.ini`
(`[auth.ldap] allow_sign_up = true`).

Source: https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-authentication/ldap/;
https://github.com/grafana/grafana/blob/main/conf/defaults.ini, `main`.

### 1f. `disable_login_form` / keeping local login for a break-glass admin

`disable_login_form`: *"Set to `true` to disable (hide) the login form, useful if you use OAuth
2.0. Default is `false`."* This is a single global switch with no per-user or per-role carve-out —
so it cannot, by itself, "hide LDAP-form login for everyone except a named local admin." A
break-glass local admin under `disable_login_form=true` would need a different UI path in (e.g.
leaving the form enabled and relying on LDAP-vs-local username routing, since Grafana tries LDAP
first and falls back to the local Grafana user store for a login that doesn't match an LDAP user).
This is a plan-level design question, not something the docs settle either way — flagged as
needing a decision, not a documented behavior.

Source: https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#disable_login_form, `latest`.

### 1g. Provisioning datasources and dashboards from files

Datasources (`/etc/grafana/provisioning/datasources/*.yaml`):

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
  - name: PostgreSQL
    type: postgres
    access: proxy
    url: postgres:5432
    database: <db>
    user: <user>
    secureJsonData:
      password: <password>
    jsonData:
      sslmode: require
```

Dashboards (`/etc/grafana/provisioning/dashboards/*.yaml`):

```yaml
apiVersion: 1
providers:
  - name: 'default'
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: true
```

`foldersFromFilesStructure: true` recreates the on-disk directory layout as Grafana folders (up to
4 levels), and requires `folder`/`folderUid` to be left unset on the provider.

Source: https://grafana.com/docs/grafana/latest/administration/provisioning/, `latest`.

### 1h. Disabling Grafana's own alerting (Alertmanager stays the one path)

`[unified_alerting] enabled` (env `GF_UNIFIED_ALERTING_ENABLED`) is still a live setting: *"Enable
or disable Grafana Alerting. The default value is `true`."* Legacy (pre-unified) alerting was
**removed entirely in v11.0.0** — so setting `enabled = false` today does not fall back to a
legacy engine, it simply turns the Alerting UI/API off. This is still the documented way to keep
Grafana out of the alert-routing business and let Alertmanager be the only path.

Sources: https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#unified_alerting
(`latest`); legacy-alerting removal per Grafana's migration docs and
https://grafana.com/blog/legacy-alerting-removal-what-you-need-to-know-about-upgrading-to-grafana-alerting/
(company blog, treated as secondary but corroborated by the `[alerting].enabled` hard-error
reported against v11.0.0 in community/ansible-collection issue #204 — flagged as
community-sourced corroboration, not itself a spec).

### 1i. Analytics / phone-home switches

From `conf/defaults.ini` (`[analytics]` section, `grafana/grafana` `main`):

- `reporting_enabled = true` — env `GF_ANALYTICS_REPORTING_ENABLED`. *"Server reporting, sends
  usage counters to stats.grafana.org every 24 hours... Change this option to false to disable
  reporting."*
- `check_for_updates = true` — env `GF_ANALYTICS_CHECK_FOR_UPDATES`. *"Set to false to disable all
  checks to https://grafana.com for new versions of grafana."*
- `check_for_plugin_updates = true` — env `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES`. Same, for
  plugin version checks.

All three default `true` and all three need to flip to `false` to fully silence outbound
version/usage calls.

Source: https://github.com/grafana/grafana/blob/main/conf/defaults.ini, `main`, checked
2026-09-03.

---

## 2. Alertmanager

Current release: **v0.34.0** (2026-08-16), image `prom/alertmanager` (Docker Hub) /
`quay.io/prometheus/alertmanager` — both documented in the upstream README as equally valid.
Source: https://github.com/prometheus/alertmanager/releases;
https://github.com/prometheus/alertmanager (README, `main`).

**`email_configs` fields**: `smarthost`, `from`, `to`, `auth_username`, `auth_password`,
`auth_password_file` (mutually exclusive with `auth_password`), `require_tls`, `tls_config`
(`ca_file`, `cert_file`, `key_file`, `server_name`, `insecure_skip_verify`), `send_resolved`,
`html`, `text`, `headers` — all as documented at
https://prometheus.io/docs/alerting/latest/configuration/#email_config.

`auth_password_file` was added by **PR #3038** ("SMTP config: add global and local password file
fields"), merged into `main` 2022-09-16 — first shipped in the next minor release after that date
(~v0.25.0, released January 2023; the PR itself doesn't carry a milestone tag, so this is inferred
from merge date vs. the release cadence, not read off a changelog line — flagged as a light
inference, not a direct citation).
Source: https://github.com/prometheus/alertmanager/pull/3038.

**`require_tls` semantics — the one surprising finding**: the docs describe it only as *"The SMTP
TLS requirement... Go does not support unencrypted connections to remote SMTP endpoints"* — which
reads as if TLS always happens somehow. Reading the notifier source removes the ambiguity:

```go
// Global Config guarantees RequireTLS is not nil.
if *n.conf.RequireTLS && !useImplicitTLS {
    if ok, _ := c.Extension("STARTTLS"); !ok {
        return true, fmt.Errorf("'require_tls' is true (default) but %q does not advertise the STARTTLS extension", ...)
    }
    ...
    if err := c.StartTLS(tlsConf); err != nil { ... }
}
```

STARTTLS is attempted **only** inside the `if *n.conf.RequireTLS` branch. With `require_tls:
false`, that whole block is skipped — Alertmanager does **not** opportunistically negotiate
STARTTLS even if the relay advertises it; it sends over the plain connection unless the
`smarthost` itself is dialed as implicit TLS (`useImplicitTLS`, i.e. a port-465-style endpoint).
For a port-25 office relay, `require_tls: false` means cleartext SMTP, full stop — this matters
for the plan since an internal-only relay might be an accepted risk, but it's not "TLS if
available."

Source: https://github.com/prometheus/alertmanager/blob/main/notify/email/email.go, `main`,
checked 2026-09-03 (lines ~184–199 in the current tree).

**v2 alerts API**: `POST /alerts` under the `/api/v2` base path (the OpenAPI spec's `basePath`),
i.e. `POST /api/v2/alerts`, body is a `postableAlerts` array; each element has `startsAt`,
`endsAt` (RFC3339 timestamps), `annotations` (a label-set object), plus the shared `alert` fields
`labels` and `generatorURL`.
Source: https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml, `main`.

**Notification metrics**: `notifications_total` and `notifications_failed_total` (both
`prometheus.CounterVec`s under the `alertmanager` namespace, i.e.
`alertmanager_notifications_total` / `alertmanager_notifications_failed_total`), labeled by
`integration` (and `reason` on the failure counter); a receiver-name label is available behind the
`--enable-feature=receiver-name-in-metrics` flag.
Source: https://github.com/prometheus/alertmanager/blob/main/notify/metrics.go, `main`.

**Routing**: `group_wait`, `group_interval`, `repeat_interval` are all settable per nested `route`
and are inherited from the parent route when omitted at a child.
Source: https://prometheus.io/docs/alerting/latest/configuration/#route, `latest`.

---

## 3. Prometheus

Current release: **v3.14.0** (2026-08-17); image `prom/prometheus` (Docker Hub) /
`quay.io/prometheus/prometheus`.
Source: https://github.com/prometheus/prometheus/releases; README.

- `--storage.tsdb.retention.time`: default `15d` if neither this flag nor
  `--storage.tsdb.retention.size` is set; accepted units `y w d h m s ms`. So
  `--storage.tsdb.retention.time=1y` is valid as written.
  Source: https://prometheus.io/docs/prometheus/latest/storage/, `latest`.
- `--web.external-url`: *"The URL under which Prometheus is externally reachable... If omitted,
  relevant URL components will be derived automatically."* `--web.route-prefix`: *"Prefix for the
  internal routes of web endpoints. Defaults to path of --web.external-url."* Neither is required
  for a loopback-only Prometheus with no reverse-proxy sub-path.
  Source: https://prometheus.io/docs/prometheus/latest/command-line/prometheus/, `latest`.
- `absent(v instant-vector)` / `absent_over_time(v range-vector)`: both return a 1-element vector
  with value `1` when the input has no series, `{}`/dropped otherwise — exactly the "no
  row/metric" shape needed for an alert on a vanished exporter or missing table row.
  Source: https://prometheus.io/docs/prometheus/latest/querying/functions/#absent, `latest`.
- `time() - <ts_metric> > 26*3600`: a plain PromQL staleness pattern (current Unix time minus a
  gauge holding a Unix timestamp, compared against a threshold); not a named function, just
  confirmed as valid PromQL against the current functions/operators doc — no special
  version-gating found.

---

## 4. node_exporter

Current release and image: README badges reference both `quay.io/prometheus/node-exporter` and
Docker Hub `prom/node-exporter`.

**Docker recipe**, verbatim from the README's own Docker Compose example:

```yaml
services:
  node_exporter:
    image: quay.io/prometheus/node-exporter:latest
    command:
      - '--path.rootfs=/host'
    network_mode: host
    pid: host
    volumes:
      - '/:/host:ro,rslave'
```

Source: https://github.com/prometheus/node_exporter (README, `master`), section "Docker".

**systemd collector**: registered `defaultDisabled` in source
(`registerCollector("systemd", defaultDisabled, NewSystemdCollector)`) — so it must be turned on
with `--collector.systemd`. `--collector.systemd.unit-include` (regex, default `.+`) and
`--collector.systemd.unit-exclude` (default excludes `automount|device|mount|scope|slice` units)
both exist. `node_systemd_unit_state` and `node_systemd_timer_last_trigger_seconds` are both
defined as metric descriptors in the current collector source
(`prometheus.BuildFQName(namespace, subsystem, "unit_state")` /
`"timer_last_trigger_seconds"`) — both are long-standing (present since the systemd collector's
early history per the CHANGELOG's systemd-related entries going back to 2016–2017); an exact
introducing PR/version for these two specific metric names was not found and is flagged as
unconfirmed on introduction date (their current existence is solid). The collector talks to
systemd over D-Bus (`github.com/coreos/go-systemd/v22/dbus`, `dbus.NewSystemdConnectionContext`);
the README's own Docker section does **not** mention a D-Bus socket mount — the
`/run/dbus/system_bus_socket` bind-mount fix is documented only in community
threads/issues (flagged as secondary), e.g. https://github.com/prometheus/node_exporter/issues/1290
and https://github.com/prometheus/node_exporter/issues/2279. A `--collector.systemd.private` flag
exists as an alternative (direct `/run/systemd/private` connection, no D-Bus) but is marked in
source as *"Strongly discouraged since it requires root. For testing purposes only."*

Source: https://github.com/prometheus/node_exporter/blob/master/collector/systemd_linux.go,
`master`.

**textfile collector**: `--collector.textfile.directory` sets the directory node_exporter reads
`.prom` files from; enabled collector, documented directly in the README's collector table and
"Textfile Collector" section.

**filesystem collector default exclude**: current default regex (source, `master`):

```
^/(dev|proc|run/credentials/.+|sys|var/lib/docker/.+|var/lib/containers/storage/.+)($|/)
```

This does **not** special-case anything under a `/host` prefix, and does not exclude a
bind-mounted host `/data` — nothing extra needs to be done to `--collector.filesystem.mount-points-exclude`
to have `/data` show up; it already isn't excluded.

Source: https://github.com/prometheus/node_exporter/blob/master/collector/filesystem_linux.go,
`master`.

---

## 5. NVIDIA DCGM exporter

Latest GitHub release: **`4.6.0-4.8.3`** (published 2026-07-15; the README's quickstart uses the
`-distroless` variant of the same tag), image `nvcr.io/nvidia/k8s/dcgm-exporter`.
Source: https://api.github.com/repos/NVIDIA/dcgm-exporter/releases;
https://github.com/NVIDIA/dcgm-exporter (README, `main`).

- **`cap_add: SYS_ADMIN`**: present in the README's own one-line quickstart
  (`docker run -d --gpus all --cap-add SYS_ADMIN --rm -p 9400:9400 ...`) — i.e. it's not an
  optional profiling-only add-on in the docs, it's baked into the default recommended invocation.
- **Embedded hostengine**: *"By default, the exporter initializes an embedded DCGM host engine
  inside its own process through `libdcgm`. It can instead connect to a separately managed DCGM
  host engine over TCP, a Unix socket, or VSOCK"* (remote via `-r`/`DCGM_REMOTE_HOSTENGINE_INFO`).
  No separate host DCGM install is required for the default (embedded) mode.
  Source: https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html.
- **Default counters file**: `etc/default-counters.csv` in the repo, installed to
  `/etc/dcgm-exporter/default-counters.csv` in the image; override with `dcgm-exporter -f
  /path/to/custom-collectors.csv`.
  Source: https://github.com/NVIDIA/dcgm-exporter (README, `main`).
- **`DCGM_FI_DRIVER_VERSION`**: documented in the DCGM fields reference as *"label, Driver
  version"* — i.e. it is a **label**, not its own time series; it decorates other DCGM field
  exports rather than appearing as `DCGM_FI_DRIVER_VERSION{...} <value>` on its own.
  Source: https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html.
- **Blackwell support / the ≥4.4.0 requirement**: DCGM release-note entries add specific Blackwell
  device IDs starting around the **4.4.x** line (e.g. GB300 NVL Galaxy / GB300 MaxQ support noted
  against 4.4.2) and continuing through **4.6.0** (new RTX PRO 5000/6000 Blackwell workstation
  device IDs, expanded Blackwell NVLink monitoring). The current dcgm-exporter tag (4.6.0-4.8.3)
  is comfortably above the ticket's ≥4.4.0 floor.
  Source: https://docs.nvidia.com/datacenter/dcgm/latest/release-notes/changelog.html.
- **CDI vs `driver: nvidia`**: the *official* Compose Deploy Specification
  (`compose-spec/compose-spec`, `deploy.md`) documents only `driver: nvidia` with `device_ids` as
  a list of GPU UUID/index strings under `deploy.resources.reservations.devices` — it does **not**
  define `cdi` as a recognized `driver` value. A `driver: cdi` +
  `device_ids: ["nvidia.com/gpu=all"]` form is used in practice (it requires Docker Engine 25+
  with the experimental CDI feature enabled, plus a generated CDI spec via `nvidia-ctk cdi
  generate`), but this is corroborated only by secondary sources (a Medium walkthrough, a Podman
  compatibility issue), not by the compose-spec's own schema doc — **flagged**: don't assume
  `driver: cdi` is portable/stable the way `driver: nvidia` is documented to be. Separately,
  the compose-spec's plain service-level `devices:` list *is* officially documented to accept a
  bare CDI qualified-device string (e.g. `devices: ["vendor1.example.com/device=gpu"]`), which is a
  different YAML shape than the `deploy.resources.reservations.devices` block.
  Sources: https://github.com/compose-spec/compose-spec/blob/main/deploy.md (`main`);
  https://github.com/compose-spec/compose-spec/blob/main/spec.md (`main`, `devices` section);
  https://docs.docker.com/compose/how-tos/gpu-support/.
- **Image size**: not independently confirmed (Docker Hub/NGC catalog page for
  `nvcr.io/nvidia/k8s/dcgm-exporter` was not reachable through the tools available in this
  session) — flagged as unverified.

---

## 6. postgres_exporter and sql_exporter

**postgres_exporter**, current release **v0.20.1** (2026-07-08), images
`quay.io/prometheuscommunity/postgres-exporter` and Docker Hub `prometheuscommunity/postgres-exporter`.

`--extend.query-path` / `PG_EXPORTER_EXTEND_QUERY_PATH` / `queries.yaml`: marked **DEPRECATED as
of v0.13.0** (2023-06-21) — CHANGELOG: *"Please note, the following features are deprecated and
may be removed in a future release: ... `extend.query-path` ... This exporter is meant to monitor
PostgresSQL servers, not the user data/databases. If you need a generic SQL report exporter
https://github.com/burningalchemist/sql_exporter is recommended."* As of the current v0.20.1
release it is still present (not removed) — no removal version found. The current README repeats
the same guidance verbatim under "Adding new metrics via a config file (DEPRECATED)."

`DATA_SOURCE_URI` + `DATA_SOURCE_PASS_FILE` (and `DATA_SOURCE_URI_FILE`) exist as documented env
vars for building the connection string with the password (or the whole URI) read from a file.

Sources: https://github.com/prometheus-community/postgres_exporter (README/CHANGELOG, `master`).

**sql_exporter**, current release **0.24.8** (2026-09-02), image `burningalchemist/sql_exporter`
(on Docker Hub, confirmed by the README's Docker Pulls badge pointing at that repo), MIT-licensed
(`LICENSE` file: "MIT License / Copyright (c) 2020 Sergei Zyubin / Copyright (c) 2017 Alin
Sinpalean").

A Postgres target is a plain DSN (`postgresql://user:pass@host:5432/db?sslmode=disable`); a
collector defining a labeled gauge from SQL looks like:

```yaml
collector_name: pricing_data_freshness
metrics:
  - metric_name: pricing_update_time
    type: gauge
    help: "Time when prices for a market were last updated."
    key_labels:
      - Market
    values: [LastUpdateTime]
    query: |
      SELECT Market, max(UpdateTime) AS LastUpdateTime
      FROM MarketPrices
      GROUP BY Market
```

Sources: https://github.com/burningalchemist/sql_exporter (README/LICENSE, `master`).

---

## 7. cAdvisor

Current release **v0.60.5** (2026-07-11). The README's own recipe:

```
ghcr.io/google/cadvisor:$VERSION # for versions prior to v0.53.0, use gcr.io/cadvisor/cadvisor instead
```

— i.e. **the image the ticket names (`gcr.io/cadvisor/cadvisor`) is the pre-v0.53.0 path**; the
current, actively-published image lives at `ghcr.io/google/cadvisor`. Docker Hub's `google/cadvisor`
repo is reported stale/deprecated in the project's own GitHub issue tracker (secondary source,
flagged: https://github.com/google/cadvisor/issues/2349) — cAdvisor is effectively a
GHCR-only image today, which matters directly for the egress allowlist.

The base `docker run` example mounts `--volume=/var/run:/var/run:rw` (**not** `:ro` as the ticket
assumed) plus `/:/rootfs:ro` and `/sys:/sys:ro`, and separately documents needing
`--privileged=true` (RHEL/CentOS, or generically to access the Docker socket) and, for perf-event
support without `--privileged`, `--volume=/dev/disk/:/dev/disk:ro --device=/dev/kmsg`.

Metrics: `container_last_seen` (Gauge, "Last time a container was seen by the exporter") and
`container_memory_working_set_bytes` (Gauge, "Current working set") both confirmed in the current
Prometheus-metrics doc.

`--disable_metrics` takes a comma-separated list (`accelerator, advtcp, app, cpu, cpuLoad,
cpu_topology, cpuset, disk, diskIO, hugetlb, memory, memory_numa, network, oom_event, percpu,
perf_event, process, referenced_memory, resctrl, sched, tcp, udp`); `--docker_only` restricts
reporting to Docker containers plus root stats.

Sources: https://github.com/google/cadvisor (docs/running.md, docs/storage/prometheus.md,
docs/runtime_options.md; `master`); https://api.github.com/repos/google/cadvisor/releases.

---

## 8. Blackbox exporter

Current release **v0.28.0** (2025-12-06), on both Docker Hub (`prom/blackbox-exporter`) and
`quay.io/prometheus/blackbox-exporter`.

Confirmed directly in source (`main.go`, `prober/http.go`):

- `probe_success` (Gauge, set 1/0 per probe outcome) — defined at the top level, shared across all
  probers.
- `probe_ssl_earliest_cert_expiry` (Gauge) — set from `getEarliestCertExpiry(resp.TLS).Unix()` on
  every HTTPS probe.
- `tls_config.server_name`: if unset in the module config, the prober fills it in from the probe
  target's host (`httpClientConfig.TLSConfig.ServerName = targetHost`) — i.e. SNI happens by
  default and `server_name` only needs to be set to *override* the target-derived value (e.g.
  probing by IP with a different SNI name).

The classic scrape-config relabel pattern for probing a target list, verbatim from the README:

```yaml
scrape_configs:
  - job_name: 'blackbox'
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
        - http://example.com:8080
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: 127.0.0.1:9115
```

Sources: https://github.com/prometheus/blackbox_exporter (README, `master`, "Prometheus
Configuration" section); https://github.com/prometheus/blackbox_exporter/blob/master/main.go;
https://github.com/prometheus/blackbox_exporter/blob/master/prober/http.go;
https://github.com/prometheus/blackbox_exporter/blob/master/example.yml (for the `http_2xx`
module shape, including `tls_config` block).

---

## 9. Caddy `/metrics` and `handle_path`

Caddy's admin API ships a `/metrics` endpoint **enabled by default with no configuration**:
*"By default, there is a `/metrics` endpoint available at the admin API (i.e.
http://localhost:2019/metrics)."* That default endpoint carries Go-runtime metrics
(`go_*`, `process_*`) and admin-API metrics (`caddy_admin_*`). The `metrics` global option (or
per-server `metrics` Caddyfile directive) is what turns on the richer per-route HTTP metrics
(`caddy_http_*`, `caddy_reverse_proxy_*`), which the docs note *"reduce performance on really busy
servers."*

The current `/docs/metrics` page's metric catalog (Runtime, Admin API, HTTP Middleware, Reverse
Proxy categories) contains **no metric with "cert" in its name and nothing describing TLS
certificate expiry** — confirming the ticket's assumption. Certificate-expiry monitoring, if
wanted, has to come from somewhere else (e.g. blackbox_exporter's
`probe_ssl_earliest_cert_expiry` against the live HTTPS endpoint, see §8).

`handle_path`: *"Works the same as the `handle` directive, but implicitly uses `uri strip_prefix`
to strip the matched path prefix."* Plain `handle` leaves the URI untouched.

Sources: https://caddyserver.com/docs/metrics; https://caddyserver.com/docs/caddyfile/directives/handle_path
(Caddy 2.11 doc tree, checked 2026-09-03 — Caddy's docs are versionless/rolling, so this reflects
whatever `latest`/2.11 currently documents rather than an archived 2.11-only snapshot).

---

## 10. Docker journald log driver

- `docker logs` continues to work with the `journald` driver — Docker reads the log back out of
  the journal for that command.
- `journalctl CONTAINER_NAME=<name>` is the documented match-field filter for a specific
  container's journal entries (example given: `sudo journalctl CONTAINER_NAME=webserver`).
- `tag`: *"Specify template to set `CONTAINER_TAG` and `SYSLOG_IDENTIFIER` value in journald
  logs."*

Source: https://docs.docker.com/engine/logging/drivers/journald/, `latest`.

---

## 11. Registry endpoints for an egress allowlist

| Image | Registry(-ies) | Additional documented hosts | Docker Hub alternative? |
|---|---|---|---|
| Grafana OSS | `docker.io` (Docker Hub) | — | native Docker Hub image |
| Alertmanager | `docker.io` / `quay.io` | — | yes (`prom/alertmanager`) |
| Prometheus | `docker.io` / `quay.io` | — | yes (`prom/prometheus`) |
| node_exporter | `docker.io` / `quay.io` | — | yes (`prom/node-exporter`) |
| DCGM exporter | `nvcr.io` only | NVIDIA's documented outbound list for NGC/driver access also names `developer.download.nvidia.com` and `repo.download.nvidia.com`; a distinct nvcr.io *blob*/CDN hostname is not independently confirmed here (flagged) | **no** |
| postgres_exporter | `docker.io` / `quay.io` | — | yes (`prometheuscommunity/postgres-exporter`) |
| sql_exporter (alt.) | `docker.io` | — | yes |
| cAdvisor | `ghcr.io` (current) / `gcr.io` (pre-v0.53.0 only) | for `gcr.io` pulls specifically, Google's own restricted-VIP guidance names `gcr.io`, `pkg.dev`, and `storage.googleapis.com`/`199.36.153.4/30` for the backing blob storage | **no** (Docker Hub `google/cadvisor` is stale) |
| blackbox_exporter | `docker.io` / `quay.io` | — | yes (`prom/blackbox-exporter`) |
| Caddy | `docker.io` (official image) | — | native Docker Hub image |

Quay.io CDN hostnames (documented for Red Hat's own quay.io-backed registries, but the CDN itself
is quay.io's, not Red Hat-specific): `cdn.quay.io`, `cdn01.quay.io`–`cdn06.quay.io`, TCP 80/443;
a `*.quay.io` wildcard is explicitly sanctioned as a simplification.
Source: https://access.redhat.com/articles/7084334.

NVIDIA (`nvcr.io`): DGX network-configuration docs list, alongside `nvcr.io` itself,
`developer.download.nvidia.com` and `repo.download.nvidia.com` (driver/CUDA downloads, not
necessarily the DCGM-exporter image pull path specifically), all over TCP 443.
Source: https://docs.nvidia.com/dgx/dgxa100-user-guide/network-config.html. **Flagged**: I could
not independently confirm a separate blob-storage hostname that `nvcr.io` image *layer* pulls
redirect through (unlike quay.io's documented CDN split) — worth a live-traffic check before
finalizing an allowlist rather than trusting this note alone.

Google (`gcr.io`): current guidance is that classic Container Registry is being retired in favor
of Artifact Registry-backed `gcr.io` repositories; Google's own restricted-private-cluster egress
doc names `gcr.io`, `pkg.dev`, and the `199.36.153.4/30`/`storage.googleapis.com` restricted-VIP
path as what needs to be reachable.
Source: https://docs.cloud.google.com/artifact-registry/docs/gke-private-clusters.

---

## 12. Grafana-managed (unified) alerting as the single alert engine

This item asks whether Grafana's own alert engine could stand in for a separate Alertmanager.
Findings below are against current Grafana OSS docs/source (rolling `latest` docs; source checked
against `grafana/grafana` `main`, 2026-09-03).

### 12a. File provisioning of alerting resources

`/etc/grafana/provisioning/alerting/*.yaml`, `apiVersion: 1`-style files, four resource kinds:

**Alert rules** (a Prometheus-datasource query and a PostgreSQL-datasource query are both just
`data` entries pointed at a different `datasourceUid`; there's no separate shape per datasource
type — Grafana wraps each query the same way and lets the datasource plugin interpret `model`):

```yaml
apiVersion: 1
groups:
  - orgId: 1
    name: my_rule_group
    folder: my_first_folder
    interval: 60s
    rules:
      - uid: my_id_1
        title: my_first_rule
        condition: A
        data:
          - refId: A
            datasourceUid: '__expr__'
            model: {}
        for: 60s
        noDataState: Alerting
        execErrState: Alerting
        labels: {team: sre_team_1}
        annotations: {some_key: some_value}
```

**Contact points** (email):

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: cp_1
    receivers:
      - uid: first_uid
        type: email
        settings:
          addresses: me@example.com;you@example.com
          singleEmail: false
```

**Notification policies** (root route + label-matched child routes):

```yaml
apiVersion: 1
policies:
  - orgId: 1
    receiver: grafana-default
    group_by: ['...']
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    matchers:
      - alertname = Watchdog
```

**Mute timings**:

```yaml
apiVersion: 1
muteTimes:
  - orgId: 1
    name: mti_1
    time_intervals:
      - times:
          - start_time: '06:00'
            end_time: '23:59'
```

**Variable interpolation**: supported (`$variable`/`$__env{}`/`$__file{}`, the same expander
mechanism as §1a) on "most properties," with a documented exception list — quoted verbatim:

> "In alerting resources, most properties support template variable interpolation, with a few
> exceptions:
> - Alert rule annotations: `groups[].rules[].annotations`
> - Alert rule time range: `groups[].rules[].relativeTimeRange`
> - Alert rule query model: `groups[].rules[].data.model`
> - Mute timings name: `muteTimes[].name`
> - Mute timings time intervals: `muteTimes[].time_intervals[]`
> - Notification template group name: `templates[].name`
> - Notification template group content: `templates[].template`"

Contact-point `settings` (where a webhook token or similar secret would live) is **not** on that
exclusion list, so it does get interpolation — meaning a `$__file{}`-delivered secret works there,
same as `bind_password` in §1a.

**Read-only in the UI**: yes, explicitly — *"You cannot edit provisioned resources from files in
Grafana. You can only change the resource properties by changing the provisioning file and
restarting Grafana or carrying out a hot reload."*

Source: https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/, `latest`.

### 12b. SMTP settings and CA trust

`[smtp]` / `GF_SMTP_*`, confirmed against both the docs and `pkg/setting/setting_smtp.go` (`main`):

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `enabled` | `GF_SMTP_ENABLED` | `false` | must be `true` to send mail |
| `host` | `GF_SMTP_HOST` | `localhost:25` | "Use port 465 for implicit TLS" |
| `from_address` | `GF_SMTP_FROM_ADDRESS` | `admin@grafana.localhost` | |
| `from_name` | `GF_SMTP_FROM_NAME` | `Grafana` | |
| `user` | `GF_SMTP_USER` | empty | |
| `password` | `GF_SMTP_PASSWORD` / `GF_SMTP_PASSWORD__FILE` | empty | the generic `__FILE` suffix from §1b applies here too |
| `startTLS_policy` | `GF_SMTP_STARTTLS_POLICY` | empty → **`OpportunisticStartTLS`** | valid values `OpportunisticStartTLS`, `MandatoryStartTLS`, `NoStartTLS` |
| `skip_verify` | `GF_SMTP_SKIP_VERIFY` | `false` | disables TLS cert verification |
| `cert_file` / `key_file` | `GF_SMTP_CERT_FILE` / `GF_SMTP_KEY_FILE` | empty | **client** cert/key for mutual TLS, not a CA trust setting |

**Default STARTTLS behavior is opportunistic**, confirmed in `pkg/services/notifications/smtp.go`:
`getStartTLSPolicy` maps `"NoStartTLS"` → the gomail constant `-1`, `"MandatoryStartTLS"` → `1`,
and *anything else including empty string* → `0`, which is `gomail.OpportunisticStartTLS` (the
library's zero-value default: *"SMTP transactions are encrypted if STARTTLS is supported by the
SMTP server. Otherwise, messages are sent in the clear."*) — the opposite default behavior from
Alertmanager's `require_tls` (§2), which defaults to *mandatory* STARTTLS and errors out if the
relay doesn't advertise it.

**No CA-trust setting exists.** The TLS config built for the SMTP dial is:

```go
tlsconfig := &tls.Config{
    InsecureSkipVerify: sc.cfg.SkipVerify,
    ServerName:         host,
}
```

with `Certificates` optionally populated from `cert_file`/`key_file` for client-cert auth — no
`RootCAs` field is ever set. Go's `crypto/tls` falls back to the process's system certificate pool
when `RootCAs` is nil, so trusting a private/internal CA for the relay's STARTTLS certificate means
installing that CA into the **container image's OS trust store** (e.g. `update-ca-certificates`)
— there is no Grafana-level `ca_file` option to hand it the CA directly, unlike Alertmanager's
`email_configs.tls_config.ca_file` (§2).

Sources: https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#smtp, `latest`;
https://github.com/grafana/grafana/blob/main/pkg/setting/setting_smtp.go,
https://github.com/grafana/grafana/blob/main/pkg/services/notifications/smtp.go, both `main`.

### 12c. Proving the channel end-to-end over HTTP

Two routes were checked directly against the current route table
(`pkg/services/ngalert/api/generated_base_api_alertmanager.go`, `main`):

- **`POST /api/alertmanager/grafana/config/api/v1/receivers/test`** — registered, handler
  `RoutePostTestReceivers`/`RoutePostTestGrafanaReceivers`. This is the real, currently-live way to
  fire a genuine test notification through a configured (or ad hoc) receiver — request body is a
  list of receiver configs (matching the `contactPoints` shape), response reports per-receiver
  pass/fail. A `Request-Timeout` header caps the wait (documented ceiling 30s in the surrounding
  API). This is confirmed as a live route in source; a stray secondary claim surfaced in web search
  ("no longer works in Grafana 13") was **not corroborated** against the actual route table and is
  treated as wrong/outdated noise, not fact.
- **`POST /api/alertmanager/grafana/api/v2/alerts`** — **does not exist.** The route table only
  registers `GET /api/alertmanager/grafana/api/v2/alerts` (and `/alerts/groups`, `/status`) for the
  built-in ("grafana") Alertmanager. The generic `POST /api/alertmanager/{DatasourceUID}/api/v2/alerts`
  route exists, but its handler (`getService` in `forking_alertmanager.go`) always resolves
  `DatasourceUID` against a **configured external Alertmanager datasource** — it has no special
  case for the literal value `grafana`, so there is no way to POST a synthetic alert straight into
  Grafana's embedded Alertmanager the way `POST /api/v2/alerts` works against a standalone
  Alertmanager (§2). Proving the pipe end-to-end against Grafana-managed alerting means either the
  receivers/test route above (tests the contact point, not the routing tree) or actually tripping a
  real alert rule (e.g. a rule with a trivially-true condition) and reading it back via
  `GET /api/alertmanager/grafana/api/v2/alerts`.
- **Auth**: HTTP Basic Auth against these routes goes through the same combined password-client
  chain as form login — confirmed in `pkg/services/authn/authnimpl/registration.go` (`main`): the
  LDAP client is always added to `passwordClients`, and the local Grafana-DB client
  (`clients.ProvideGrafana`) is added too **unless `disable_login` is set**; both are wrapped into
  one `clients.ProvidePassword(...)` chain, and — if `[auth.basic] enabled` (default `true`) — that
  combined chain backs `clients.ProvideBasic`. So a local break-glass admin's username/password
  keeps working over HTTP Basic Auth even with LDAP enabled, because Basic Auth isn't LDAP-only —
  it tries LDAP, then the local user store, in one call.
- Listing current alert state to confirm a firing alert: `GET /api/alertmanager/grafana/api/v2/alerts`
  (registered per above) is the way to read back active alerts against the built-in Alertmanager.

Sources: https://github.com/grafana/grafana/blob/main/pkg/services/ngalert/api/generated_base_api_alertmanager.go,
https://github.com/grafana/grafana/blob/main/pkg/services/ngalert/api/forking_alertmanager.go,
https://github.com/grafana/grafana/blob/main/pkg/services/ngalert/api/api_alertmanager.go,
https://github.com/grafana/grafana/blob/main/pkg/services/authn/authnimpl/registration.go,
https://github.com/grafana/grafana/blob/main/pkg/services/authn/clients/basic.go,
https://github.com/grafana/grafana/blob/main/pkg/services/authn/clients/password.go — all `main`,
checked 2026-09-03.

### 12d. Grafana's own alerting metrics

The meta-monitoring doc names, among others: `grafana_alerting_notification_latency_seconds`
(histogram, "the number of seconds taken to send notifications for firing and resolved alerts");
`alertmanager_notifications_total` and `alertmanager_notifications_failed_total` — **the same
metric names as standalone Alertmanager** (§2), which makes sense since Grafana's embedded
Alertmanager is built on the same `notify` package internals; a counter of rule evaluations by
state (normal/pending/alerting/nodata/error); a gauge of scheduled alert rules; and a counter of
failed writes to the alert-state-history backend.

Source: https://grafana.com/docs/grafana/latest/alerting/set-up/meta-monitoring/, `latest`.

### 12e. A "dead man's switch" for the alert engine itself

Grafana's documented pattern (in the Grafana Cloud IRM integration doc) is a Grafana-managed alert
rule with an always-true condition — *"select a Prometheus data source and set the query to
`vector(1) > 0`"* — with a pending period shorter than the desired heartbeat interval and a repeat
interval shorter than that too, routed to a dedicated contact point. That gives the *sending* half
for free with plain Grafana alerting. The *missing-heartbeat detection* half — noticing when the
pings themselves stop arriving — is documented specifically as a **Grafana Cloud IRM** feature (an
external receiver that pages when its expected heartbeat goes silent); nothing in Grafana OSS
itself watches for the absence of its own notifications. An OSS-only stack needs an external
watcher (a `healthchecks.io`-style pinger, or a GIDEON-side cron/CLI check) to close that loop —
flagged as a design gap, not a documented OSS feature.

Source: https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/configure-irm/, `latest`
(the heartbeat/dead-man's-switch section is explicitly scoped to Grafana OSS/Enterprise
*connecting to* Grafana Cloud IRM).

### 12f. `noDataState: Alerting` as an `absent()` equivalent

Confirmed: *"If the pending period is `0`, the alert instance transitions immediately to
**Alerting**. Otherwise, it transitions to the **Pending** state, and then to **Alerting** after
the pending period has elapsed and the last evaluation returns no data."* The four states are (doc
labels, not the raw YAML enum names): "Set No Data state" (default), "Set Alerting state," "Set
Normal state," "Keep last state." Setting `noDataState: Alerting` on a rule is Grafana's
`absent()`-equivalent — an empty query result is itself treated as an alertable condition.

Source: https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/state-and-health/, `latest`.

---

## What this changes for the plan

- Grafana's LDAP `bind_password` can be delivered as a file (`$__file{/path}`), matching the
  "secrets as files, never env vars" rule — confirmed at the source level even though the LDAP
  docs page only shows the `$__env{}` form.
- A user who authenticates via LDAP but matches none of the configured `group_mappings` is
  refused login and disabled by Grafana itself (`ErrInvalidCredentials` + `IsDisabled=true`) — no
  extra denial logic is needed on top of the group mapping list, but the mapping list needs an
  explicit catch-all only if "silently disabled" is not the desired behavior for stray AD users.
- The Caddy `/grafana/` route and Grafana's `serve_from_sub_path` must be chosen as a matched
  pair: `handle_path` (strips) pairs with the default `serve_from_sub_path=false`; plain `handle`
  (keeps the prefix) pairs with `serve_from_sub_path=true`. Picking one without the other breaks
  the sub-path.
- `disable_login_form` is all-or-nothing — it cannot by itself scope "hide LDAP form, keep local
  break-glass login." The break-glass admin story needs a different mechanism (e.g. leave the form
  up and rely on Grafana's built-in LDAP-then-local auth fallback) rather than this flag.
- Alertmanager's `require_tls: false` does not mean "TLS if the relay offers it" — it means
  plaintext SMTP unless the smarthost is dialed as implicit TLS. If the office relay on port 25
  doesn't speak STARTTLS, the plan needs to decide explicitly between accepting cleartext on an
  internal segment or failing closed with `require_tls: true` (the default).
- cAdvisor's current upstream image is `ghcr.io/google/cadvisor`, not `gcr.io/cadvisor/cadvisor`
  (that path stopped receiving new versions at v0.53.0) — the egress allowlist needs `ghcr.io`,
  and cAdvisor is not available on Docker Hub as a live alternative.
- DCGM exporter's default invocation already assumes `--cap-add SYS_ADMIN`; the compose-spec's
  officially documented device syntax for GPUs is `driver: nvidia` + `device_ids`, and the
  `driver: cdi` shape used in some NVIDIA-adjacent examples is not part of that spec's documented
  schema — treat it as an experimental/Engine-version-dependent path, not a guaranteed one, when
  deciding which form the renderer emits.
- postgres_exporter's `--extend.query-path` custom-queries mechanism is deprecated (since v0.13.0,
  2023) but not removed as of the current release — it can still be used for the audit_log-derived
  gauges the ticket needs, but the upstream project's own recommendation is `sql_exporter` for new
  work, which is worth weighing against the extra image.
- Caddy exposes no certificate-expiry metric of its own at any config level; TLS-expiry monitoring
  has to come from blackbox_exporter's `probe_ssl_earliest_cert_expiry` against the live HTTPS
  endpoint (or a separate cert-watching tool), not from Caddy's `/metrics`.
- A separate Alertmanager is still worth keeping: Grafana-managed alerting can provision rules,
  contact points, policies, and mute timings from files and can email through the same SMTP
  settings, but it has no `POST /api/v2/alerts` equivalent for injecting a synthetic alert into its
  own built-in Alertmanager (only a receiver-test route, which proves the contact point but not the
  routing tree), no CA-trust setting for the relay's certificate (Alertmanager's `tls_config.ca_file`
  has no Grafana analog — the CA has to live in the image's OS trust store instead), and no
  built-in dead-man's-switch receiver (the documented pattern's receiving half is a Grafana Cloud
  IRM feature). If `gideon alerts test` needs to post a synthetic alert and assert it was routed
  and delivered — not just that a contact point can send — a standalone Alertmanager with its
  `POST /api/v2/alerts` route is the more direct path to that proof; Grafana-managed alerting is a
  viable *replacement* only if the plan is willing to redefine "prove the channel" as "successfully
  run the receivers/test call" instead.

## Erratum (2026-09-03, found during `F_0.0.22` implementation)

§12c above is wrong for the pinned Grafana. At tag `v13.0.2` (and on `main`)
`POST /api/alertmanager/grafana/config/api/v1/receivers/test` is registered
only to answer **410 Gone**; its swagger block reads "This endpoint has been
removed. Please use
`/apis/notifications.alerting.grafana.app/v1beta1/namespaces/{namespace}/receivers/{uid}/test`
instead" (`pkg/services/ngalert/api/tooling/definitions/alertmanager.go`,
`v13.0.2`). The "no longer works in Grafana 13" claim the note dismissed was
right. The replacement is the Kubernetes-style receivers API:
`GET /apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers`
lists receivers (`spec.title`, `spec.integrations[]` with `uid`, `type`,
`version`, `settings`, `secureFields`, `disableResolveMessage`; the object's
`metadata.name` is the uid the test route takes), and
`POST .../receivers/{uid}/test` takes
`{"integration": {uid, type, version, disableResolveMessage, settings, secureFields}, "alert": {"labels": {...}, "annotations": {...}}}`
and answers `{"status": "success" | "failure", "duration": "...", "error": "..."}`
(`apps/alerting/notifications/pkg/apis/alertingnotifications/v1beta1/receiver_createreceiverintegrationtest_*_gen.go`,
`v13.0.2`). Org 1's namespace is `default`
(`grafana/authlib` `types/namespace.go`, `OrgNamespaceFormatter`).
