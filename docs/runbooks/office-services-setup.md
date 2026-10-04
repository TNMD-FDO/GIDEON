# Office-services setup for GIDEON

A CSA prepares the office services in these five sections outside the box
before `./preflight.sh` can pass. Record each site value in
`/etc/gideon/site.yaml` and place each supplied secret as a file under
`/etc/gideon/secrets/`. Do this after the first host provision run and before
preflight, as described in
[`docs/runbooks/install-upgrade.md`](install-upgrade.md) §1.

Each section starts with the requirement and the preflight row that checks it,
then gives the setup steps. A paragraph headed **TNMD example** shows how the
office that first ran GIDEON met the requirement with its own equipment;
substitute your own. No script performs these steps: the CSA does them, and
preflight checks the result.

## 1. Active Directory

GIDEON needs LDAPS to the domain in `auth.ldap.host`, a bind account that can
read users, groups, and `memberOf`, the users and admins groups, and a
`userPrincipalName` on every account that will sign in. The domain
controllers must already serve LDAPS on port 636 (`auth.ldap.port`) with a
certificate §4 describes. Preflight's `ldap` row binds, resolves the
configured groups, and checks every users-group member through `memberOf` for
a UPN.

Create a read-only bind account such as `svc-gideon-ldap` in the office's
service-accounts OU or the default `Users` container. Set its User logon name
to a UPN such as `svc-gideon-ldap@example.org`. In `auth.ldap.bind_user`,
give the account name alone and GIDEON binds as
`<auth.ldap.bind_user>@<auth.ldap.host>`; where the UPN's suffix differs from
the domain, give the full UPN or the account's DN instead.
Generate a long random password in the office password manager first. Set
**Password never expires** and **User cannot change password**; leave
**must-change-at-next-logon** off. Add no group memberships beyond the
`memberOf` grant below. Default Domain Users read access is enough for the
ordinary bind and group searches, provided the account can also read
`memberOf` as described below. Leave `auth.ldap.search_base` at its default,
the whole domain, unless every signing-in account sits under a narrower base;
a group given by its CN alone is looked up as
`CN=<name>,CN=Users,<auth.ldap.search_base>`, so a narrowed base needs the
groups' full DNs.

Place the password at `/etc/gideon/secrets/ldap_bind_password`. `ldapsearch -y`
reads that file verbatim, so it must have no trailing newline. The directory
is root-owned and mode 0700; run this Bash command from the box, entering the
password at the hidden prompt. The value is passed on stdin, never as a
command argument:

```bash
(
  set -e
  set -o pipefail
  umask 077
  IFS= read -rs -p 'LDAP bind password: ' bind_password
  printf '\n'
  printf '%s' "$bind_password" | sudo tee /etc/gideon/secrets/ldap_bind_password >/dev/null
  unset bind_password
)
```

After the write succeeds, run `sudo python3 -m gideon host provision` again.
Its `secrets-dirs` step converges the supplied file to `root:gideon` mode 0440.

Create two Global Security groups. `auth.ldap.users_group` defaults to
`GIDEON-Users`: direct membership permits sign-in. `auth.ldap.admins_group`
defaults to `GIDEON-Admins`: direct membership grants GIDEON admin access via
`gideon users reconcile` and is the only directory group allowed into Grafana
(§5). Add staff's normal AD accounts to the users group; put admins in
**both** groups, because users-group membership is still the sign-in gate.

If either group, or a group in `auth.ldap.mirror_groups`, lives outside AD's
default `Users` container, put its full distinguished name in the
corresponding site key rather than only its CN. Look it up in PowerShell with
`Get-ADGroup GIDEON-Users | Select-Object DistinguishedName`, changing the
group name for each lookup. Preflight refuses a group it cannot resolve.

Every signing-in account needs ADUC's **User logon name** on the Account tab,
for example `user@example.org`; accounts created by scripts may lack it.
People sign in with `sAMAccountName`, while the frontend, reconcile, and audit
records identify them by UPN. The E-mail (`mail`) attribute is not required or
read by GIDEON. Preflight refuses a users-group member with no UPN, and also
refuses a member the bind account cannot see through `memberOf`: check for a
delegation that misses an OU or a member outside `auth.ldap.search_base`.

The bind account must be able to read the `memberOf` back-link on user
objects; the frontend login filter and `gideon users reconcile` use it.
Domains that removed *Authenticated Users* from *Pre-Windows 2000 Compatible
Access* can hide the attribute. Grant the bind account read access by adding
it to *Pre-Windows 2000 Compatible Access* or by delegating *Read memberOf* on
the OU that holds the signing-in accounts. Preflight's `ldap` row refuses
until it can see all users-group members this way.

## 2. DNS

The box's resolvers must answer the directory's zone: the site `hostname`
must resolve to the box, `auth.ldap.host` to the domain controllers, and
`backup.target.host` to the target if it is a name. Preflight's `hostname`
row runs `getent hosts` for the site hostname; verify that its answer is the
box's address. The `ldap` and `backup-ssh` rows exercise the other names.

Create the box and backup-target records in the zone's authoritative DNS, for
example `gideon.example.org` and `nas.example.org`; the directory's domain
name must resolve to its controllers. Add an associated PTR where a reverse
zone exists. Keep static server records from being dynamically updated, and
use the zone's default TTL.

If caching resolvers sit in front of the authoritative servers, put the
records in the authoritative zone regardless. A lookup made before a record
existed can cache NXDOMAIN for the zone's negative TTL (an hour on AD
defaults). The symptom is `dig @dc1.example.org gideon.example.org` answering
while `getent hosts gideon.example.org` on the box fails. Flush **every**
caching resolver, then run `sudo resolvectl flush-caches` on the box. Set the
caches' upstreams to the domain controllers, or forward the directory zone to
them; a public upstream may answer NXDOMAIN for that zone.

**TNMD example — Pi-hole in front of AD DNS.** Put the zone's records in AD
DNS, never in Pi-hole's "Local DNS records". On each Pi-hole, use Settings →
Flush DNS cache or run `pihole restartdns`, then flush the box's cache as
above. Pi-hole's upstreams are the domain controllers or forward their zone
to them.

## 3. Backup target

Preflight's `backup-ssh` row and `backup push` need an SSH account named by
`backup.target.user` (default `gideon-backup`) on `backup.target.host`, at
port 22; GIDEON has no backup-target port setting. Authorize the box's backup
public key for that account. Set `backup.target.path` to one directory on one
filesystem that supports hard links; create it, writable by the account. The
target needs `sh`, `rsync`, and a `sha256sum` that accepts `-c -` on stdin; preflight
probes authentication, path writability, and those tools. Push sends no owner
or group (`--no-owner --no-group`); each set's manifest carries ownership and
modes for restore. The account's login shell must run `sh -c` commands.

Supported targets include Synology DSM 7, QNAP, TrueNAS SCALE, and plain
Linux over SSH. On plain Linux the account is an ordinary user with no
special privilege; a NAS whose SSH service admits only administrators needs
the account in that group (the example below). A Windows file server is not
supported. Choose a target with headroom for the retained snapshots; an
optional quota can protect other data on its volume. If the target is a
replicated pair, push to the primary. Pruning must really delete data: a
trash or recycle folder on the share would hoard every pruned snapshot. The
path must be available unattended after a reboot; an encrypted volume
awaiting manual unlock would fail the nightly push. Enable data
checksums where the filesystem offers them and monitor the target's disk
health, because the target must keep a sound backup copy.

Under `backup.target.path`, each push has a directory named by its UTC time,
hard-linked to the previous push. The same name with `.partial` appended
marks a push in flight; old pushes are pruned after `backup.remote_days`.
Each completed directory holds `push.json`, the sets, and the pgBackRest
repository as of that push.

After `host provision` prints the backup public key, run this on the box,
substituting the account and host from `/etc/gideon/site.yaml`:

```bash
sudo ssh-copy-id \
  -i /etc/gideon/secrets/backup_ssh_key.pub \
  -o UserKnownHostsFile=/etc/gideon/backup_known_hosts \
  "<backup.target.user>@<backup.target.host>"
```

The command asks for the target account's password once. Confirm the target
host key at first contact, comparing the fingerprint shown with the one the
target's own console reports (`ssh-keygen -lf` on its host key): this places
it in `/etc/gideon/backup_known_hosts`, the trust file `backup push` uses. If the
target does not permit password login, place the printed public-key line in
that account's `~/.ssh/authorized_keys` by the target's own means and confirm
its host key in the same trust file. If key authentication still fails on an
OpenSSH target, check the target account's permissions:
`chmod 755 ~; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`. Once the
key is placed, GIDEON never uses the account's password, so password login
may be disabled for it.

**TNMD example — Synology DSM 7 on a DS2422.** Choose one DSM volume; its
shared folder path starts with `/volumeN/`, where N is the volume number,
such as `/volume3/gideon-backup`. Prefer a volume with headroom isolated from
other duties; TNMD's units also provide office DNS. In Control Panel → Shared
Folder, create `gideon-backup` on that volume, turn **Recycle Bin** off, and
keep encryption off unless policy requires it and unattended mounting is
arranged. On btrfs, enable data checksums when creating the folder;
compression is optional because much of the payload is already compressed.
Give the `gideon-backup` user Read/Write on this folder and everyone else No
access. Create the user with a strong password for initial key placement and
put it in `administrators`, because DSM's SSH service admits administrators;
deny access to all other shares and applications. A quota is optional.
Enable the user home service so `~/.ssh` exists after first login, enable SSH
on port 22, and enable the separate rsync service. DSM may otherwise answer
`Permission denied, please try again.` to `rsync --server` even after SSH key
authentication succeeds. Grant the account the rsync application privilege
if that permission wall appears; rsync's port 873 is not used and may remain
firewalled.

## 4. Certificates

Preflight's `ldap` row needs the CA root that signs the domain controllers'
LDAPS certificates at `/etc/gideon/ca.pem`. GIDEON connects to the
directory by the domain name in `auth.ldap.host`, so each domain
controller's LDAPS certificate must carry that name among its SANs, beside
the controller's own name; a controller without such a certificate is
issued and given one by the office CA's own means before preflight. Install
also needs a TLS certificate for the site `hostname`, with the private key
generated on the box and never sent to the CA. Its homes are `/etc/gideon/tls/cert.pem` (0644) and
`/etc/gideon/secrets/tls_key`, whose owner and mode the next
`sudo python3 -m gideon host provision` converges to `root:gideon` 0440, as
for every supplied secret.

`/etc/gideon/ca.pem` is one PEM file. Where the CA issues from an
intermediate, the file holds the root followed by each intermediate, since
GIDEON verifies every chain against this file alone. `cert.pem` holds the
site certificate first; append the intermediates after it so browsers
receive the whole chain.

Export the trusted CA root as PEM from the office's CA or a machine that
trusts it, and copy the PEM text to the box. Before installing it, verify a
domain controller's live LDAPS certificate against that file:

```bash
openssl verify -CAfile root-ca.pem \
  <(openssl s_client -connect <auth.ldap.host>:636 </dev/null | openssl x509)
```

Then install the PEM with
`sudo install -m 0644 root-ca.pem /etc/gideon/ca.pem`.

Generate the site key and CSR on the box, using the site `hostname` as both
subject and SAN; the key stays there:

```bash
openssl req -new -newkey rsa:3072 -nodes -keyout ~/gideon-tls.key \
  -out ~/gideon-tls.csr -subj "/CN=<hostname>" \
  -addext "subjectAltName=DNS:<hostname>"
```

Submit the CSR to the office CA and bring back the issued certificate as
`~/gideon-tls.pem`, never the key. Ask for a lifetime of months, a year or
more being usual: GIDEON alerts when fewer than 14 days remain, so a shorter
certificate pages from the start. Before installing, check **all three** on the box — the subject and
SAN name `hostname`, the chain verifies against `/etc/gideon/ca.pem`, and
the two public-key digests are equal:

```bash
openssl x509 -in ~/gideon-tls.pem -noout -subject -ext subjectAltName
openssl verify -CAfile /etc/gideon/ca.pem ~/gideon-tls.pem
openssl x509 -in ~/gideon-tls.pem -noout -pubkey | sha256sum
openssl pkey -in ~/gideon-tls.key -pubout | sha256sum
```

Install the certificate and key at their fixed homes, then remove the loose
key copy:

```bash
sudo install -m 0644 ~/gideon-tls.pem /etc/gideon/tls/cert.pem
sudo install -m 0400 ~/gideon-tls.key /etc/gideon/secrets/tls_key
shred -u ~/gideon-tls.key
sudo python3 -m gideon host provision
```

To renew, repeat this section's key, CSR, check, and install steps, then run
`sudo python3 -m gideon tls reload`.

**TNMD example — an office CA on AD CS.** On a domain-joined Windows machine,
export the root from its trust store with PowerShell, one command at a time:

```powershell
$ca = Get-ChildItem Cert:\LocalMachine\Root | ? Subject -like "*<your-CA-CN>*" | Sort NotAfter -Descending | Select -First 1
$ca | Export-Certificate -FilePath $env:TEMP\ca.cer
certutil -encode $env:TEMP\ca.cer $env:USERPROFILE\Desktop\root-ca.pem
```

Publish the Web Server template and grant Enroll if this CA has not issued
one before. On the Windows machine, submit the CSR and encode the issued
certificate:

```powershell
certreq -submit -attrib "CertificateTemplate:WebServer" gideon-tls.csr gideon-tls.cer
certutil -encode gideon-tls.cer gideon-tls.pem
```

That template's default validity is two years.

## 5. The mail relay, Grafana's sign-in, and egress

The office relay must accept mail from the box and from the sender in
`alerts.smtp.from`, sending to `alerts.recipients[]` through
`alerts.smtp.host` and `alerts.smtp.port` (25 by default; a submission port
such as 587 is set there). Preflight's `smtp` row sends one test message
through it. Configure the relay to offer STARTTLS: preflight and Grafana use
it when offered and require it when `alerts.smtp.user` is set, so credentials
never travel in the clear. Its certificate must chain to a CA the box trusts,
either a public CA or the office CA at `/etc/gideon/ca.pem`, which Grafana
receives in its trust directory. A self-signed relay certificate makes
`gideon alerts test` fail with a TLS error; fix the relay's certificate,
not TLS verification.

When `alerts.smtp.user` is set, put its password in
`/etc/gideon/secrets/smtp_password`. Use the same Bash command in §1,
changing the prompt to `SMTP password: ` and the `sudo tee` destination to
`/etc/gideon/secrets/smtp_password`; it likewise writes no trailing newline.
Run `sudo python3 -m gideon host provision` after the write to converge its
owner and mode.

Members of the admins group (`auth.ldap.admins_group`), and only they, sign
in to `https://<hostname>/grafana/` with their directory accounts. Other users
are refused at the form and left as disabled Grafana records. The local
`grafana-admin` account is for a broken directory.

The box's outbound HTTPS destinations are listed in `config/egress.yaml` in
the release's checkout; allow them through the office firewall. Among them,
the DCGM exporter image is published only on `nvcr.io` and cAdvisor comes
from GHCR, both needed by `registry mirror`.

With `web.search` on, SearXNG and the frontend's page loader reach search
engines and result pages, directly or through `egress_proxy`. This is the
runtime path by which user-authored text leaves the box, and it runs only
after a user turns on search in General and confirms the reminder. Returned
destinations vary, so no fixed allowlist names them; `web.search: off`
removes the feature and its service.
