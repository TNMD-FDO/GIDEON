---
verified_against:
  - pin: host.gh_runner
    version: v2.337.0
---
# Disabling self-update on GIDEON's self-hosted runner

Scope: `actions/runner` v2.337.0, Linux x64, `/opt/gh-runner`, systemd service via `svc.sh`,
org-level registration at `TNMD-FDO`, runner name `gideon`.

Primary sources: `actions/runner` source at tag `v2.337.0`
(`https://raw.githubusercontent.com/actions/runner/v2.337.0/...`) and docs.github.com.
Where noted, findings are corroborated by directly inspecting GIDEON's live installation at
`/opt/gh-runner` (read-only, on the box this note was written on) — that box is not a GitHub
source but is treated as ground truth for "what our runner actually did."

---

## 1. The `.runner` settings file

**Class serialized:** `RunnerSettings`, `[DataContract]`, in
`src/Runner.Common/ConfigurationStore.cs` lines 14–161. The `DisableUpdate` member:

```csharp
[DataMember(EmitDefaultValue = false)]
public bool DisableUpdate { get; set; }
```
— `src/Runner.Common/ConfigurationStore.cs` lines 35–36.

**JSON member casing — `disableUpdate`:** the C# property is `DisableUpdate` (PascalCase); the
`[DataContract]`/`[DataMember]` attributes carry no explicit `Name`, so the wire casing is
whatever `StringUtil`'s serializer settings produce. Serialization goes through
`StringUtil.ConvertToJson`/`ConvertFromJson` in `src/Runner.Sdk/Util/StringUtil.cs` lines 12–18,
35–38, 68–71, which use `Newtonsoft.Json.JsonConvert` with
`new VssJsonMediaTypeFormatter().SerializerSettings` (from the `GitHub.Services.WebApi` NuGet
package, not part of the `actions/runner` repo — **its contract resolver is not verifiable from
this repo's source**). I could not find a `.runner` JSON fixture inside `actions/runner`'s own
test tree that pins the casing. **Flag on this claim:** the `disableUpdate` casing is corroborated
by two indirect, non-authoritative signals rather than proven from `actions/runner` source alone:
  - The runner's own broker-poll query parameter for the identical concept is written by hand
    (not through the JSON serializer) as `disableUpdate` — `src/Sdk/WebApi/WebApi/BrokerHttpClient.cs`
    line 100 (`queryParams.Add("disableUpdate", ...)`), showing the team's own naming convention
    for this boolean.
  - A GitHub Community discussion (secondary source, flagged as such) quotes an actual `.runner`
    file containing `"disableUpdate": true`:
    https://github.com/orgs/community/discussions/50112
  - I additionally confirmed on GIDEON's live `.runner` (registered *without* `--disableupdate`)
    that the key is **absent** — consistent with `EmitDefaultValue = false` (see next point) but
    this does not by itself prove the *casing* used when the value is `true`.

**Omitted when false:** yes. `EmitDefaultValue = false` on every `RunnerSettings` member (lines
17, 20, 23, 26, 29, 32, 35, 38, 41, 44, 47, 50, 53, 56, 59) means a `bool` member serializes only
when non-default, i.e. `DisableUpdate` is written only when `true`. Confirmed empirically: GIDEON's
`/opt/gh-runner/.runner` (registered without the flag) has no `disableUpdate` key at all:

```
﻿{
  "agentId": 6,
  "agentName": "gideon",
  "poolId": 1,
  "poolName": "Default",
  "serverUrl": "https://pipelinesghubeus13.actions.githubusercontent.com/...",
  "gitHubUrl": "https://github.com/TNMD-FDO",
  "workFolder": "_work",
  "useV2Flow": true,
  "serverUrlV2": "https://broker.actions.githubusercontent.com/"
}
```

**UTF-8 BOM:** yes, and here is where it comes from. `IOUtil.SaveObject` —
`src/Runner.Sdk/Util/IOUtil.cs` line 42 — writes with
`File.WriteAllText(path, StringUtil.ConvertToJson(obj), Encoding.UTF8)`. .NET's
`Encoding.UTF8` static instance has a non-empty preamble (the 3-byte BOM `EF BB BF`), and
`File.WriteAllText`/the `StreamWriter` it uses emit that preamble at the start of a new file, so
every runner-written JSON config file (`.runner`, `.runner_migrated`, `.credentials`,
`.credentials_migrated`) gets a BOM. Confirmed on GIDEON's box: `xxd .runner | head -1` →
`efbb bf7b 0a20 2022 6167 656e 7449 6422 ...` (BOM immediately followed by `{`).
`ConfigurationStore.GetSettings()`/`GetMigratedSettings()` read the file back with
`File.ReadAllText(path, Encoding.UTF8)` (`ConfigurationStore.cs` lines 287, 306), which strips a
detected BOM transparently.

**Config-time log line:** confirmed. `CommandSettings.TestFlag` —
`src/Runner.Listener/CommandSettings.cs` lines 448–462 — logs
`_trace.Info($"Flag '{name}': '{result}'");` (line 460) for every flag test, generic across all
flags. The flag's literal string name is `"disableupdate"` (lowercase, no hyphen) —
`src/Runner.Common/Constants.cs` line 142: `public static readonly string DisableUpdate = "disableupdate";`.
So a `./config.sh --disableupdate ...` run logs `Flag 'disableupdate': 'True'` to the diag trace.
`DisableUpdate` is a valid flag only for the `configure` command —
`src/Runner.Listener/CommandSettings.cs` lines 32–36 (`validOptions[Commands.Configure]` includes
`Flags.DisableUpdate`); it is not in the `remove` or `run` option lists, so it cannot be passed to
`config.sh remove` or `run.sh`. `Runner.cs`'s own `--help` text documents it the same way:
`--disableupdate        Disable self-hosted runner automatic update to the latest released version`
— `src/Runner.Listener/Runner.cs` line 1151.

---

## 2. `.runner_migrated`

**File name and path:** `.runner_migrated` at the runner root —
`src/Runner.Common/HostContext.cs` lines 456–460
(`WellKnownConfigFile.MigratedRunner` → `Path.Combine(RootFolder, ".runner_migrated")`).
Sibling well-known files, same method, for reference: `.runner` (450–454), `.credentials`
(462–466), `.credentials_migrated` (468–472), `.credentials_rsaparams` (474–478), `.service`
(480–484).

**What it is / why it exists:** it is *not* a legacy-format artifact of a schema migration. It's
the local cache for a server-driven **config refresh** mechanism
(`src/Runner.Listener/RunnerConfigUpdater.cs`). When the runner receives a config-refresh signal
naming `configType: "runner"`, `UpdateRunnerSettingsAsync` (lines 77–130) base64-encodes the
current `.runner` content, exchanges it with the service over a `configRefreshUrl`, decodes the
service's response into a fresh `RunnerSettings` object, validates the returned `AgentId`/
`AgentName` still match the current runner (lines 112–125), and — if all checks pass — calls
`_store.SaveMigratedSettings(refreshedRunnerConfig)` (line 128), which writes it via the same
`RunnerSettings`/`IOUtil.SaveObject` path as `.runner` (`ConfigurationStore.cs` lines 363–376), so
it has the identical BOM/UTF-8/`EmitDefaultValue=false` behavior. There's an analogous
credentials-refresh path writing `.credentials_migrated` (`SaveMigratedCredential`,
`ConfigurationStore.cs` lines 333–346; caller `UpdateRunnerCredentialsAsync`,
`RunnerConfigUpdater.cs` line 213).

**Carries `disableUpdate` too:** yes — `.runner_migrated` deserializes into the same
`RunnerSettings` class as `.runner` (`GetMigratedSettings()`, `ConfigurationStore.cs` lines
299–316, returns `RunnerSettings`), so it is governed by the identical `[DataMember(EmitDefaultValue
= false)]` rule on `DisableUpdate`. Confirmed on GIDEON's box: `.runner_migrated` is byte-for-byte
identical in content to `.runner` (same BOM, same fields, `disableUpdate` absent because the
runner was registered without the flag).

**Preferred over `.runner` at listener start when present:** yes. In
`src/Runner.Listener/Runner.cs` `RunAsync` (lines 407–480): the listener first tries
`configManager.LoadMigratedSettings()` (line 434); if that succeeds it attempts to create a
session using the migrated settings *first* (lines 446–470); only if there is no migrated file, it
fails to load, or session creation with it fails, does the listener fall back to the original
`.runner` settings (lines 472–476, "Falling back to original .runner settings").

**Deleted by `config.sh remove` along with the others:** yes, but note the exact mechanics.
`config.sh remove` → `ConfigurationManager.UnconfigureAsync` (lines 527–618) → after removing the
agent from the server, calls `DeleteLocalRunnerConfig()` (line 609; also the same method used
directly by `remove --local`, see §4), which calls:
  - `_store.DeleteCredential()` — `ConfigurationStore.cs` lines 378–382 — deletes **both**
    `.credentials` and `.credentials_migrated` (`IOUtil.Delete` on `_credFilePath` and
    `_migratedCredFilePath`), plus, separately in `DeleteLocalRunnerConfig`
    (`ConfigurationManager.cs` lines 505–506), `keyManager.DeleteKey()` deletes
    `.credentials_rsaparams` — confirmed in `RSAFileKeyManager.DeleteKey()`
    (`src/Runner.Listener/Configuration/RSAFileKeyManager.cs` lines 63–69, keyed to
    `WellKnownConfigFile.RSACredentials` at line 92); `RSAFileKeyManager` is the Linux/non-Windows
    default (`IRSAKeyManager.cs` line 13, `[ServiceLocator(Default = typeof(RSAFileKeyManager))]`
    under `#else`/non-`OS_WINDOWS`).
  - `_store.DeleteSettings()` — `ConfigurationStore.cs` lines 389–393 — deletes **both** `.runner`
    and `.runner_migrated` (`IOUtil.Delete` on `_configFilePath` and `_migratedConfigFilePath`).

  `.service` is **not** deleted by this path. It's deleted only by the service-uninstall step
  (see §4) — `svc.sh`'s `uninstall` function removes `.service`
  (`src/Misc/layoutbin/systemd.svc.sh.template` lines 117–131, specifically the `rm "${CONFIG_PATH}"`
  at line 127, `CONFIG_PATH=.service` at line 21). Confirmed byte-identical against GIDEON's live
  `/opt/gh-runner/svc.sh` (same line numbers, same logic).

---

## 3. What the listener does with `DisableUpdate` at runtime

`RunnerSettings.DisableUpdate` is read from the loaded settings object and threaded into **every**
poll for new work, on both the legacy pool-based flow and the current broker (`UseV2Flow`) flow —
it is not read anywhere else in the listener:

- Legacy: `MessageListener.GetNextMessageAsync` (`src/Runner.Listener/MessageListener.cs`) calls
  `_runnerServer.GetAgentMessageAsync(..., _settings.DisableUpdate, ...)` at line 258. `RunnerServer`
  (`src/Runner.Common/RunnerServer.cs` lines 41, 278–281) forwards it straight through to the
  generated `_messageTaskAgentClient.GetMessageAsync(...)`.
- Broker: `MessageListener.cs` line 282, and `BrokerMessageListener.cs` line 301, call
  `_brokerServer.GetRunnerMessageAsync(..., _settings.DisableUpdate, ...)`. The broker HTTP client
  (`src/Sdk/WebApi/WebApi/BrokerHttpClient.cs`, `GetRunnerMessageAsync`, lines 59–108) builds the
  `GET .../message` request and, when `disableUpdate != null`, appends the query parameter
  literally as `disableUpdate=<true|false>` (line 100: `queryParams.Add("disableUpdate",
  disableUpdate.Value.ToString().ToLower())`). This confirms the exact wire format asked about.

`DisableUpdate` is *also* set on the server-side `TaskAgent` record at configure time
(`ConfigurationManager.cs` line 652 `agent.DisableUpdate = disableUpdate;` in `UpdateExistingAgent`,
and line 688 in `CreateNewAgent`) — so the flag is persisted twice: once in the local `.runner`
settings, and once as an attribute of the runner's server-side registration record. At configure
time the runner cross-checks the two and fails loudly if they disagree
(`ConfigurationManager.cs` lines 312–316 and 370–374:
`throw new NotSupportedException("The GitHub server does not support configuring a self-hosted
runner with 'DisableUpdate' flag.")` — this only fires if the *server* doesn't honor
`command.DisableUpdate` when it's `true`, not if it's `false`; there's no equivalent complaint if
the server silently *drops* a caller's `false`, only if it drops a `true`).

**What happens to an update message when the flag is set:** I could not find any client-side gate
in the runner source. `AgentRefreshMessage` handling in `Runner.cs` (lines 608–638, matching on
`message.MessageType == AgentRefreshMessage.MessageType`) and the broker equivalent
`RunnerRefreshMessage` (lines 648–654) unconditionally kick off `ISelfUpdater.SelfUpdate(...)` if
such a message *arrives* — there is no `if (!_settings.DisableUpdate)` guard anywhere in that code
path. The mechanism, as far as the client source shows, is that the `disableUpdate=true` query
parameter on every poll tells the (closed-source) GitHub Actions service not to *send* a refresh
message in the first place. **Flag:** I cannot verify the server-side suppression logic itself —
that code isn't public — but this reading is consistent with docs.github.com's description (§6)
that a disabled-auto-update runner simply stops being sent updates and instead risks losing job
eligibility after 30 days, which only makes sense if the server, not the client, is what withholds
the update message.

---

## 4. `config.sh remove`

**Registration token vs. removal token — two distinct REST endpoints, confirmed both in source and
in docs:**
- `POST /orgs/{org}/actions/runners/registration-token` — token expires after **one hour**;
  "Returns a token that you can pass to the config script." docs.github.com REST API reference,
  "Create a registration token for an organization":
  https://docs.github.com/en/rest/actions/self-hosted-runners?apiVersion=2022-11-28#create-a-registration-token-for-an-organization
- `POST /orgs/{org}/actions/runners/remove-token` — token expires after **one hour**; "Returns a
  token that you can pass to the config script to remove a self-hosted runner from an
  organization." Same page, "Create a remove token for an organization":
  https://docs.github.com/en/rest/actions/self-hosted-runners?apiVersion=2022-11-28#create-a-remove-token-for-an-organization
  Both require the caller to have admin access to the organization; for OAuth/classic PATs, the
  `admin:org` scope.

  The runner's own source builds exactly this URL shape when using a GitHub PAT to fetch a token
  non-interactively: `ConfigurationManager.GetJITRunnerTokenAsync`
  (`src/Runner.Listener/Configuration/ConfigurationManager.cs` lines 738–754) constructs
  `.../orgs/{org}/actions/runners/{tokenType}-token` where `tokenType` is literally `"registration"`
  or `"remove"` (passed in from `GetRunnerTokenAsync`, lines 710–736, called with `"registration"`
  at line 153 during configure and `"remove"` at lines 562/579 during unconfigure) — matching the
  REST paths above exactly.

**Is a registration token accepted for removal?** The runner CLI does not itself distinguish token
*kinds* beyond which endpoint it asks for when a PAT is supplied (it always asks the matching
`{tokenType}-token` endpoint) or which prompt it shows for a manually-pasted `--token`
(`GetRunnerRegisterToken` prompts "What is your runner register token?",
`GetRunnerDeletionToken` prompts "Enter runner remove token:" — `CommandSettings.cs` lines
271–287). Whether the GitHub backend actually accepts a *registration* token value on the
*remove* call is server-side validation not visible in this repo; I could not verify it from
primary source and am flagging it as unconfirmed. Docs.github.com's removal instructions describe
only "an automatically-generated, time-limited token" without naming it, so they don't settle this
either — see below.

**`--local`:** confirmed to skip the server entirely. `Runner.cs` lines 167–175: if
`command.RemoveLocalConfig` (i.e. `--local`) is set, `config.sh remove` calls
`configManager.DeleteLocalRunnerConfig()` directly and returns success — it never contacts GitHub,
never needs any token, and (notably) does **not** check whether the systemd service is still
installed (that check lives only inside `UnconfigureAsync`, not in `DeleteLocalRunnerConfig`). It
deletes `.credentials`(+`.credentials_migrated`+`.credentials_rsaparams`) and
`.runner`(+`.runner_migrated`) as described in §2, leaving the agent record live on the server.

**Documented order for a service install (Linux):** confirmed —
docs.github.com, "Configuring the self-hosted runner application as a service"
(https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
sections "Stopping the service" (`sudo ./svc.sh stop`) and "Uninstalling the service" (first stop
if running, then `sudo ./svc.sh uninstall`). The source-level reason this order is *required*, not
just recommended, for a full `config.sh remove`: `ConfigurationManager.UnconfigureAsync`
(`src/Runner.Listener/Configuration/ConfigurationManager.cs` lines 527–550) checks
`_store.IsServiceConfigured()` (true whenever the `.service` file exists) and, on any non-Windows
platform, **refuses** outright:
```csharp
#if OS_WINDOWS
    ... serviceControlManager.UnconfigureService(); ...
#else
    // unconfig systemd or osx service first
    throw new Exception("Uninstall service first");
#endif
```
— lines 540–549 (throw at line 548). So on Linux, `config.sh remove` (without `--local`) hard-fails
with `Uninstall service first` unless `svc.sh stop` + `svc.sh uninstall` have already run and
deleted `.service`.

**Refuses when already configured — confirmed, exact message:**
`ConfigurationManager.ConfigureAsync`, `src/Runner.Listener/Configuration/ConfigurationManager.cs`
lines 124–127:
```csharp
if (IsConfigured())
{
    throw new InvalidOperationException("Cannot configure the runner because it is already
    configured. To reconfigure the runner, run 'config.cmd remove' or './config.sh remove' first.");
}
```
This check runs unconditionally at the top of `ConfigureAsync`, before any `--replace` handling is
reached, and is keyed purely off the *local* `.runner` file's existence
(`ConfigurationStore.IsConfigured()`, lines 236–242). This means `--replace` is **not** a way to
reconfigure a runner that is still locally configured — `config.sh` will refuse before it ever gets
that far, regardless of `--replace`.

**What `--replace` actually does (org level):** it comes into play only when the local `.runner`
file is absent (e.g. after `config.sh remove --local`) but the *server* still has an agent with the
same name. During configure, the runner looks up existing agents by name
(`GetAgentsAsync`/`GetRunnerByNameAsync`, `ConfigurationManager.cs` lines 265–276); if a match is
found and the user confirms `--replace` (line 280), `UpdateExistingAgent` (lines 640–674) mutates
the *existing* `TaskAgent` object in place — new public key, `agent.Version`, `agent.Ephemeral`,
**`agent.DisableUpdate = disableUpdate`** (line 652), relabeling — and the runner then calls
`ReplaceRunnerAsync`/`ReplaceAgentAsync` (lines 289, 309) which reuses the **same agent id** on the
server (`agent.Id = runner.Id;`, line 293, from the server's response), rather than creating a
second, new agent record. So yes: at the org level, `--replace` with the same runner name replaces
the existing server-side registration (same identity, updated attributes) instead of creating a
duplicate.

---

## 5. Token lifetimes and where a CSA gets them

- **Web UI, adding a runner:** Settings → Actions → Runners → **New runner** → **New self-hosted
  runner** (docs.github.com, "Adding self-hosted runners":
  https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners — note
  the button sequence is now "New runner" then "New self-hosted runner," not a single button). The
  page states: "The `config` script requires the destination URL and an automatically-generated
  time-limited token to authenticate the request. **The token expires after one hour.**" — this is
  the registration token, matching the REST doc's one-hour figure in §4.
- **Web UI, removing a runner:** Settings → Actions → Runners → click the runner's name → **Remove**
  (docs.github.com, "Removing self-hosted runners":
  https://docs.github.com/actions/hosting-your-own-runners/removing-self-hosted-runners, section
  "Removing a runner from an organization"). The doc's exact wording: "The instructions include the
  required URL and an automatically-generated, time-limited token." — the doc page does not use the
  literal phrase "removal token"/"remove token" itself (only the REST reference does), but the
  mechanism and one-hour lifetime match the `remove-token` endpoint in §4.
- **Via `gh api` (or any REST client):**
  ```
  gh api --method POST /orgs/TNMD-FDO/actions/runners/registration-token
  gh api --method POST /orgs/TNMD-FDO/actions/runners/remove-token
  ```
  Both need admin access to the `TNMD-FDO` org (a classic PAT needs `admin:org`; a fine-grained
  token needs organization self-hosted-runners write access, per the same REST page).

- **Same-machine, no-portal alternative worth flagging:** the removing-self-hosted-runners doc also
  documents a fallback for when you don't have access to the org's UI/API but do have shell access
  to the runner machine: delete the `.runner` file directly to let the machine be re-registered
  without redownloading the runner package. That is *not* an equivalent to a full removal — it
  leaves the server-side agent record orphaned (the same "automatically removed after 14 days of no
  connection" rule from the same page would eventually clean it up) — so it is not the
  recommended path for GIDEON's re-registration but is worth knowing about.

---

## 6. The 30-day manual-update obligation

docs.github.com, "Self-hosted runners reference"
(https://docs.github.com/en/actions/reference/runners/self-hosted-runners), section "Runner
software updates on self-hosted runners":

- "To turn off automatic software updates and install software updates yourself, specify the
  `--disableupdate` flag when registering your runner using `config.sh`."
- "If you disable automatic updates, you must still update your runner version regularly...you
  will be required to update your runner version within 30 days of a new version being made
  available."
- "If you do not perform a software update within 30 days, the GitHub Actions service will not
  queue jobs to your runner."
- "If a critical security update is required, the GitHub Actions service will not queue jobs to
  your runner until it has been updated" — i.e. the 30-day grace period does not apply to
  critical/security releases; the service can enforce sooner.

**Time-sensitive addendum (as of 2026-09-02, today's date) — flagging because it materially
affects GIDEON's operating window:** GitHub's changelog announced active, dated enforcement of
runner version minimums layered on top of the above 30-day rule:
https://github.blog/changelog/2026-06-12-github-actions-minimum-version-enforcement-timeline-for-self-hosted-runners/
- Full enforcement for GitHub Enterprise Cloud (which covers github.com org-hosted runners like
  `TNMD-FDO`'s): **September 25, 2026**, preceded by ~4 weeks of intermittent "brownout" blocking
  of registration and job execution on outdated runners.
  - Registration minimum version: **2.329.0** or later (GIDEON's 2.337.0 clears this).
  - Job-execution requirement restated as: "The runner must stay up to date by installing each new
    runner release within 30 days of its publication" — i.e. this is the same 30-day rule from the
    reference doc above, now being actively enforced rather than merely documented.
  - Explicit statement on disabled auto-update: "Runners with auto-update disabled must be upgraded
    manually on a regular cadence."
This means once GIDEON disables auto-update, someone/something must track new `actions/runner`
releases and update the runner within 30 days of each one, and that obligation is about to become
strictly enforced (brownouts from roughly late August 2026, full enforcement Sept 25, 2026) rather
than a soft policy. **Flag:** this is a GitHub Blog changelog post, not a docs.github.com reference
page — treated here as primary (official first-party announcement) but it is time-sensitive news,
not a stable spec; re-check closer to the date if this note is consulted later.

**How GitHub enforces it:** per the reference doc, enforcement is "the GitHub Actions service will
not queue jobs to your runner" — i.e., job dispatch is withheld from the runner's pool assignment;
this is consistent with §3's finding that the client sends `disableUpdate=true` on every poll and
the client itself contains no capability to inspect or act on version-gating — enforcement is
entirely server-side and outside what's visible in `actions/runner` source.

---

## 7. Reading the installed runner's version

- **CLI:** `bin/Runner.Listener --version` (also `config.sh --version`, `run.sh --version`, same
  flag). Source: `Runner.cs` lines 98–100:
  ```csharp
  if (command.Version)
  {
      _term.WriteLine(BuildConstants.RunnerPackage.Version);
      ...
  }
  ```
  Confirmed on GIDEON's box: `sudo -u gh-runner ./bin/Runner.Listener --version` and
  `./config.sh --version` both print `2.337.0`.
- **Where the version comes from:** `src/runnerversion` at the repo root is the single-line source
  of truth (`2.337.0` at this tag) that the build pipeline stamps into
  `src/Runner.Sdk/BuildConstants.cs` at build time. The checked-in `BuildConstants.cs` is a
  placeholder (`Version = "0"`, `PackageName = "N/A"`) carrying the comment: "WARNING: This file is
  automatically regenerated on layout so the runner can provide version/commit info (do not
  manually edit it)." — confirming the compiled `Runner.Sdk.dll` embeds the real version string,
  not the checked-out source file. Confirmed on GIDEON's box via `strings bin/Runner.Sdk.dll`,
  which contains the literal string `2.337.0+397b032cbf865e9c3ddfab89d533ec19325e1273` (version +
  commit hash, matching the `--commit` flag's separate output).
- **No loose version file ships in the release tarball:** confirmed by listing the contents of
  GIDEON's cached `actions-runner-linux-x64-2.337.0.tar.gz` — no `runnerversion`/`VERSION`-style
  file at the tarball root or in `bin/`; the only "version" hits are unrelated Node.js internal
  headers (bundled `externals/node20`, `externals/node24`) and an unrelated .NET
  `System.Diagnostics.FileVersionInfo.dll`. The version is only recoverable via the `--version`/
  `--commit` flags (or by extracting the string from the compiled `Runner.Sdk.dll`).

---

## Practical implication for GIDEON's re-registration

Not itself a citation-bearing "fact" from the sources, but the operational shape those facts
imply, worth recording so the wizard/runbook doesn't have to re-derive it:

`config.sh` refuses to reconfigure a locally-configured runner outright (§4), and `config.sh
remove` (without `--local`) refuses on Linux while the systemd service is installed (§4). So two
source-supported paths exist to add `--disableupdate` to the already-registered `gideon` runner:

1. **Full remove + re-add** (needs a *removal* token, §4/§5):
   `sudo ./svc.sh stop && sudo ./svc.sh uninstall` → `./config.sh remove --token <removal token>`
   → `./config.sh --url https://github.com/TNMD-FDO --token <registration token> --name gideon
   --disableupdate ...` → reinstall/start the service.
2. **Local-only remove + `--replace`** (needs only a *registration* token, §4): `./config.sh remove
   --local` (works even with the service still installed/running, per source, though stopping it
   first avoids the service racing to relaunch `Runner.Listener` against files being deleted) →
   `./config.sh --url https://github.com/TNMD-FDO --token <registration token> --name gideon
   --disableupdate --replace ...`, which updates the same server-side agent record (same id) rather
   than creating a new one.

Both end with the same effect: a fresh `.runner` containing `"disableUpdate": true` and the
server-side `TaskAgent.DisableUpdate` set to match (§3), and the obligation described in §6 begins
from that point.
