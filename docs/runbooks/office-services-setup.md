# Office-services setup for GIDEON (CSA runbook material)

What a CSA sets up **outside the box** before `./preflight.sh` can pass: the
before-you-begin checklist made explicit. Written from TNMD's first live setup
(2026-09-01); promote into the formal install runbook when that document lands.
TNMD example values throughout — a receiving office substitutes its own and
records them in `/etc/gideon/site.yaml`.

The `~/gideon-onbox-wizard.sh` pattern (an interactive script that walks these
steps and places the results) is worth regenerating per office; this checklist
makes the before-you-begin work explicit for the person preparing a box.

## 1. Active Directory (ADUC)

**Service account** — `svc-gideon-ldap`, in your service-accounts OU (default
Users container is fine):

- **User logon name (UPN) matters**: the box binds as
  `svc-gideon-ldap@<ad-domain>` (e.g. `svc-gideon-ldap@example.org`).
- Password: generated long/random in the office password manager **first**,
  then set here. Flags: ☑ Password never expires, ☑ User cannot change
  password, ☐ must-change-at-next-logon.
- **No extra group memberships** — default Domain Users read access is all the
  bind and group searches need. The account only ever does LDAP reads.

**Groups** — two Global Security groups, names exactly matching the site
file's `auth.ldap` keys (defaults shown):

| Group | Meaning |
|---|---|
| `GIDEON-Users` | membership = may log in to GIDEON |
| `GIDEON-Admins` | membership = GIDEON admin (`gideon users reconcile` enforces); since `v0.0.22` also the only directory group that may open Grafana at `/grafana/` (§7) |

Add office staff (their normal AD accounts) to `GIDEON-Users`; put admins in
**both** groups — the users group is the login gate, so an admin outside it is
an admin who can't log in.

**Every account that will sign in needs a `userPrincipalName`** — ADUC's *User
logon name* on the Account tab (`user@<ad-domain>`), which every account
created through ADUC already has; only script-created accounts may lack one,
and preflight's LDAP check refuses a users-group member without one. This is
required because the frontend and reconcile need a stable directory identity.
Users sign in with their `sAMAccountName`; the frontend keys the
account by its UPN and shows it as the account's address, and
`gideon users reconcile` joins directory accounts to frontend users by the same
attribute. The E-mail (`mail`) attribute is not read by GIDEON.

## 2. DNS (AD DNS on the DCs)

Two A records in the AD forward zone (DNS Manager on a DC):

- `gideon` → the box's LAN IP, which is the certificate subject and Caddy's
  hostname.
- `nas` → the backup target's LAN IP.

Per record: "create associated PTR" if a reverse zone exists (harmless
either way); **leave "allow any authenticated user to update…" unchecked**
(static server records must not be dynamically claimable); default TTL.

**If the office resolvers are Pi-hole (TNMD: Pi-hole on the Synologys) or any
caching layer in front of AD DNS:**

- Records go in **AD DNS regardless** — it is authoritative for the AD zone;
  the cache forwards to it. Never split AD-zone names into Pi-hole "Local DNS
  records".
- **Negative caching gotcha**: any lookup of the name *before* the record
  existed plants an NXDOMAIN in the caches for the zone's negative TTL (an
  hour on AD defaults). Symptom: `dig <name> @<dc>` answers while
  `getent hosts <name>` on the box refuses. Fix: flush DNS caches on **every**
  Pi-hole (Settings → Flush DNS cache, or `pihole restartdns`), then
  `sudo resolvectl flush-caches` on the box.
- Determinism check while you're in Pi-hole: upstreams should be the DCs (who
  forward externally), or at minimum the AD zone conditionally forwarded to
  them — public upstreams answering NXDOMAIN for the private zone cause
  maddening intermittent failures.

## 3. Backup target (Synology DSM 7 — TNMD: DS2422)

**Which unit/volume**: any single volume works — a DSM volume is one
filesystem, so `--link-dest` can hard-link snapshots under one path. Choose by headroom,
and prefer a volume that isolates backup growth from other duties the unit
carries (TNMD's Synologys also run office DNS). If the units replicate
primary→backup, target the **primary** — GIDEON's snapshots then gain a
second copy for free. **Whatever you choose, `backup.target.path` in
`site.yaml` must match** (`/volumeN/<share-name>` — TNMD:
`/volume3/gideon-backup`).

**Shared folder** (Control Panel → Shared Folder → Create):

- Name `gideon-backup` (it becomes the path's last segment); location = the
  chosen volume.
- **Recycle Bin: OFF** — pruning uses `rsync --delete`; the Synology Recycle Bin would
  invisibly hoard every pruned snapshot.
- Encryption: off unless policy demands (an unmounted encrypted share after a
  NAS reboot silently fails the nightly push).
- On btrfs: **enable data checksums** (creation-time only option) — integrity
  is this share's entire purpose. Compression optional (payload is largely
  pre-compressed).
- Permissions: `gideon-backup` user Read/Write; everyone else No access.

**Account** (Control Panel → User & Group → Create): `gideon-backup`, strong
password (used exactly once, for key placement).

- **Member of `administrators` — required**: Synology's sshd only allows SSH
  for administrators. Compensate by stripping everything
  else: No access to all other shares, **Deny all** on the Applications tab.
  (If a later `backup push` hits a permission wall, the "rsync" application
  privilege is the first knob — do not pre-grant.)
- No quota required; a quota is a reasonable hard cap protecting the volume's
  other tenants.

**Three service toggles**: User & Group → Advanced → ☑ Enable user home service
(no home ⇒ no `~/.ssh`; DSM creates the home on first login). Terminal & SNMP
→ ☑ Enable SSH service, **port 22** (the product has no port setting — 22 is
assumed by preflight and `backup push`), Advanced Settings left at default.
File Services → rsync → ☑ **Enable rsync service** — DSM's `rsync` binary is a
wrapper that answers `Permission denied, please try again.` to `rsync
--server` (the push, over SSH) while the service is off, even though SSH key
authentication succeeded; the first push showed that enabling SSH alone does
not enable this separate rsync service. The rsync
port 873 itself is not used and may stay firewalled. Then grant the account the
**rsync application privilege** (User & Group → `gideon-backup` → Applications).

**What the target must have** (preflight's `backup-ssh` check proves all of
it): `rsync`, `sha256sum` that accepts `-c -` on stdin, and `sh`. The push
sends no ownership (`--no-owner --no-group`, a plain account cannot store it);
every set's manifest carries the owners and modes and a restore reapplies them.

**Layout on the share**: one directory per push, named by its UTC time
(`20260902T210803Z`), hard-linked to the previous one; `<name>.partial` while
in flight; pruned after `backup.remote_days`. Each holds `push.json` (its
coverage record), the sets, and the pgBackRest repository as of that push.

**Key placement**: done from the box, not by hand —
`ssh-copy-id -i /etc/gideon/secrets/backup_ssh_key.pub` (the wizard runs it),
which also triggers home creation. First contact enrolls the NAS host key
into `/etc/gideon/backup_known_hosts` — the shared trust file `backup push`
uses. If key auth fails afterwards: the classic DSM cause is permissions —
`chmod 755 ~; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys` on the NAS.

## 4. Certificates (AD CS)

**CA root → `/etc/gideon/ca.pem`** (required for preflight's LDAPS check).
Export from any domain-joined machine — the root is already in its trust
store. PowerShell, one command at a time:

    $ca = Get-ChildItem Cert:\LocalMachine\Root | ? Subject -like "*<your-CA-CN>*" | Sort NotAfter -Descending | Select -First 1
    $ca | Export-Certificate -FilePath $env:TEMP\ca.cer
    certutil -encode $env:TEMP\ca.cer $env:USERPROFILE\Desktop\root-ca.pem

Copy the PEM text to the box. Sanity check before installing: the DC's live
LDAPS cert must verify against it
(`openssl verify -CAfile root-ca.pem <(openssl s_client -connect <ad-domain>:636 </dev/null | openssl x509)`).

**TLS certificate for `gideon.<zone>`** (needed by install, not preflight).
The private key is generated **on the box** and never travels:

    openssl req -new -newkey rsa:3072 -nodes -keyout ~/gideon-tls.key \
      -out ~/gideon-tls.csr -subj "/CN=gideon.<zone>" \
      -addext "subjectAltName=DNS:gideon.<zone>"

Submit the CSR on a Windows machine (Web Server template — publish it and
grant Enroll if the CA has never issued one):

    certreq -submit -attrib "CertificateTemplate:WebServer" gideon-tls.csr gideon-tls.cer
    certutil -encode gideon-tls.cer gideon-tls.pem

Verify on the box before installing: subject/SAN, chain
(`openssl verify -CAfile /etc/gideon/ca.pem`), and key match (pubkey sha256 of
cert vs key). Homes: cert → `/etc/gideon/tls/cert.pem` (0644), key →
`/etc/gideon/secrets/tls_key` (0400) — then `shred -u` the loose key copy.
Web Server template default validity is 2 years; the product alerts when
fewer than 14 days remain, and renewal is the same CSR flow and `gideon tls reload`.

## 5. Hard-won practicalities

- **Terminal paste hygiene**: PowerShell mangles pasted multi-line commands —
  run them one line at a time. Values prompted by wizards/scripts are plain
  text: never include the backticks/quotes that chat or docs use as
  formatting.
- **tmux → workstation clipboard**: OSC 52 (`set -s set-clipboard on` +
  `set -as terminal-features ",*:clipboard"`) if the client terminal honors
  it; the universal fallback is **Shift+drag** (terminal-native selection),
  then Ctrl+Shift+C / auto-copy depending on the terminal.
- **A silent, long-running wizard step may be a *stopped* process**, not a
  slow one (a stray Ctrl-Z suspends it): `ps` showing `T` state confirms;
  `fg` or `kill -CONT <pid>` resumes.
- The LDAP bind password file is written **without a trailing newline**
  (`ldapsearch -y` reads the file verbatim) — relevant to anyone placing it
  by hand instead of via the wizard.

## 1a. Active Directory — two facts the first live pass added

- **Group location**: if `GIDEON-Users` / `GIDEON-Admins` (or a mirrored group)
  live anywhere but the default `Users` container, set the site keys to the
  groups' full distinguished names (`Get-ADGroup GIDEON-Users | select
  DistinguishedName`); preflight refuses with that fix otherwise.
- **`userPrincipalName` on every signing-in account** (see §1 above): the
  identity the frontend, reconcile, and the audit log key a person by; `mail`
  is not required. Preflight refuses naming any users-group member without one,
  and any member it cannot see through `memberOf` (a per-OU delegation that
  misses an OU, or a member outside `auth.ldap.search_base`).
- **`memberOf` must be readable by the bind account.** Open WebUI's login
  filter and `gideon users reconcile` select users by `memberOf`, a back-link
  AD only shows to accounts allowed to read it (domains that removed
  *Authenticated Users* from *Pre-Windows 2000 Compatible Access* hide it).
  Grant `svc-gideon-ldap` read of `memberOf` on user objects — add it to
  *Pre-Windows 2000 Compatible Access*, or delegate *Read memberOf* on the
  users OU. Preflight's LDAP check refuses until it can see the users group's
  members that way.

## 1b. One-time re-key of accounts created before v0.0.12

Frontend accounts created under `v0.0.11` were keyed by the `mail` attribute.
From `v0.0.12` the frontend keys accounts by the UPN, so an old row no longer
matches its person: the next sign-in would create a second account under the
UPN and the old row would drift to `pending` (its chats and knowledge bases
stay with it). Re-key each such account once, **before its person signs in
again**:

1. Sign in as the break-glass admin (`gideon-admin@gideon.invalid`; the
   password is in the office password manager).
2. Admin panel → Users → the account's edit (pencil) → set **Email** to the
   account's UPN exactly as the directory has it
   (`Get-ADUser <sAMAccountName> | select UserPrincipalName`) → Save. The
   frontend updates both its auth row and its user row, so the user id
   survives and chats, knowledge bases, and the audit rows already written stay
   attached.
3. Verify: `sudo python3 -m gideon users reconcile` (bare) prints no
   `would … → pending` line for the account, and the person signs in on the
   LDAP form and lands on the same account.

Deleting the row (Admin panel → Users → delete) is the fallback only for an
account with nothing worth keeping. A receiving office never needs this
section: its accounts are UPN-keyed from the first sign-in.

## 7. The SMTP relay, Grafana's door, and one more egress host (`v0.0.22`)

- **The relay's STARTTLS and certificate.** Preflight's SMTP check already sends
  one message; from `v0.0.22` Grafana sends every page-class alert through the
  same relay (`alerts.smtp.{host, port, from}`, `alerts.recipients[]`). Grafana
  uses STARTTLS when the relay offers it, and **requires** it when
  `alerts.smtp.user` is set (the credentials never travel in the clear — the
  same fail-closed rule preflight applies). The relay's certificate must chain
  to a CA the box trusts: a public CA, or the office CA at
  `/etc/gideon/ca.pem`, which Grafana is handed in its trust directory. A relay
  presenting a self-signed certificate fails `gideon alerts test` with the
  relay's TLS error; fix the relay, never verification.
- **Who opens Grafana.** Members of `GIDEON-Admins` (and only they) sign in at
  `https://<hostname>/grafana/` with their directory account. Anyone else is
  refused at the form and left as a disabled Grafana record — expected. The
  local break-glass administrator (`grafana-admin`) is for a broken directory.
- **Egress.** The DCGM exporter image is published only on `nvcr.io`; the
  office firewall must allow it (HTTPS) for `registry mirror` and the CI
  runner, beside the hosts already in `config/egress.yaml`. cAdvisor comes from
  GHCR, already allowed.
- **Search egress** (`v0.1.25`). With `web.search` on, the box reaches the internet's search engines and fetches result pages, directly or through `egress_proxy`, from the `searxng` container and the frontend's page loader — the one runtime path user-authored text leaves the box, and only after a user has turned search on in General and confirmed the reminder. The destinations are whatever the engines return, so no allowlist names them; `web.search: off` removes the feature and the service.
