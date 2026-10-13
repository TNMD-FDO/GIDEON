#!/usr/bin/env bash
# Move the populated package-default store before the Docker volume becomes active.
set -euo pipefail

old=/var/lib/containerd
new=/var/lib/docker/containerd
record=/var/lib/gideon-store-move
packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
policy_created=0

cleanup_policy() {
    if [[ $policy_created -eq 1 ]]; then
        rm -f /usr/sbin/policy-rc.d
    fi
}
trap cleanup_policy EXIT

pending_packages() {
    local status
    if grep -Eq '^Inst (docker-ce|docker-ce-cli|containerd.io|docker-buildx-plugin|docker-compose-plugin)( |$)' "$record/packages.simulated"; then
        return 0
    else
        status=$?
        if [[ $status -eq 1 ]]; then return 1; fi
        echo "could not read the Docker package simulation (grep exit $status)" >&2
        return "$status"
    fi
}

if [[ -e "$record" || -L "$record" ]]; then
    echo "store-move record already exists at $record; inspect it before continuing" >&2
    exit 1
fi
if [[ -e /usr/sbin/policy-rc.d || -L /usr/sbin/policy-rc.d ]]; then
    echo '/usr/sbin/policy-rc.d already exists; refusing to replace host policy' >&2
    exit 1
fi
test -d "$old"
install -d -m 0700 "$record"

survey() {
    df -h / /var/lib/docker
    du -sb "$old"
    bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.before'
    bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.before'
    docker system df
    cp -p /etc/containerd/config.toml "$record/config.toml.shipped"
}

survey
echo 'survey complete; image and container lists saved'
apt-get update -q
apt-get -s install "${packages[@]}" > "$record/packages.simulated"
if pending_packages; then
    apt-get install -y "${packages[@]}"
    survey
    apt-get -s install "${packages[@]}" > "$record/packages.simulated"
    if pending_packages; then
        echo 'Docker package simulation still has pending actions; daemons remain running' >&2
        exit 1
    fi
else
    status=$?
    if [[ $status -ne 1 ]]; then exit "$status"; fi
fi
echo 'package simulation clear'

systemctl stop docker.socket docker.service
systemctl stop containerd.service

expect_inactive() {
    local unit output status
    unit=$1
    if output=$(systemctl is-active "$unit" 2>&1); then status=0; else status=$?; fi
    if [[ $status -ne 3 || "$output" != inactive ]]; then
        echo "$unit was not confirmed inactive (exit $status; $output)" >&2
        return 1
    fi
}

expect_inactive docker.socket
expect_inactive docker.service
expect_inactive containerd.service
if output=$(pgrep -a containerd-shim 2>&1); then
    echo "containerd-shim processes remain: $output" >&2
    exit 1
else
    status=$?
    if [[ $status -ne 1 || -n "$output" ]]; then
        echo "could not confirm containerd-shim stopped (exit $status; $output)" >&2
        exit 1
    fi
fi
if output=$(findmnt -R "$old" 2>&1); then
    if [[ -n "$output" ]]; then
        echo "mounts remain under $old: $output" >&2
        exit 1
    fi
else
    status=$?
    if [[ $status -ne 1 || -n "$output" ]]; then
        echo "could not inspect mounts under $old (exit $status; $output)" >&2
        exit 1
    fi
fi
tmpmounts=$(ls -A "$old/tmpmounts")
if [[ -n "$tmpmounts" ]]; then
    echo "containerd temporary mounts remain: $tmpmounts" >&2
    exit 1
fi
echo 'daemons stopped; no shims, mounts, or temporary entries remain'

mkdir -p "$new"
rsync -aHAXxS --numeric-ids "$old/" "$new/"
echo 'store copy complete'

dry_output=$(rsync -aHAXxS --numeric-ids -n -i "$old/" "$new/")
if [[ -n "$dry_output" ]]; then
    echo "store dry pass found differences: $dry_output" >&2
    exit 1
fi

inode_old=$(find "$old" -print | wc -l)
inode_new=$(find "$new" -print | wc -l)
bytes_old=$(du -sb "$old" | awk '{print $1}')
bytes_new=$(du -sb "$new" | awk '{print $1}')
whiteout_old=$(find "$old/io.containerd.snapshotter.v1.overlayfs/snapshots" -type c -print | wc -l)
whiteout_new=$(find "$new/io.containerd.snapshotter.v1.overlayfs/snapshots" -type c -print | wc -l)
opaque_counts=$(python3 -c '
import os, sys
for root in sys.argv[1:]:
    count = 0
    for base, dirs, _ in os.walk(root):
        for name in dirs:
            try:
                os.getxattr(os.path.join(base, name), "trusted.overlay.opaque")
                count += 1
            except OSError:
                pass
    print(count)
' "$old/io.containerd.snapshotter.v1.overlayfs/snapshots" "$new/io.containerd.snapshotter.v1.overlayfs/snapshots")
readarray -t opaque_rows <<< "$opaque_counts"
if [[ ${#opaque_rows[@]} -ne 2 ]]; then
    echo 'could not read both opaque-marker counts' >&2
    exit 1
fi
opaque_old=${opaque_rows[0]}
opaque_new=${opaque_rows[1]}
printf 'counts inode=%s/%s bytes=%s/%s whiteouts=%s/%s opaque=%s/%s\n' \
    "$inode_old" "$inode_new" "$bytes_old" "$bytes_new" \
    "$whiteout_old" "$whiteout_new" "$opaque_old" "$opaque_new"
if [[ $inode_old != "$inode_new" || $bytes_old != "$bytes_new" || \
    $whiteout_old != "$whiteout_new" || $opaque_old != "$opaque_new" ]]; then
    echo 'store dry pass or one of its four counts differs; daemons remain stopped and both roots remain' >&2
    exit 1
fi
echo 'store dry pass and four counts equal'

if [[ -e /usr/sbin/policy-rc.d || -L /usr/sbin/policy-rc.d ]]; then
    echo '/usr/sbin/policy-rc.d already exists; refusing to replace host policy' >&2
    exit 1
fi
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d
policy_created=1
set +e
# The daemons are stopped, so GIDEON's co-tenant guard cannot list running
# containers and needs the disruption acknowledged, as the runbook's line does.
(cd /opt/gideon && python3 -m gideon host provision --no-gpu --only docker-engine --acknowledge-disruption)
provision_status=$?
set -e
rm /usr/sbin/policy-rc.d
policy_created=0
if [[ $provision_status -ne 0 ]]; then
    echo "GIDEON docker-engine provision exited $provision_status; both roots remain" >&2
    exit "$provision_status"
fi
echo 'docker-engine provision complete; temporary package policy removed'

for unit in docker.socket docker.service containerd.service; do
    if ! systemctl is-active --quiet "$unit"; then
        echo "$unit is not active after provision" >&2
        exit 1
    fi
done
config=$(containerd --config /etc/containerd/config.toml config dump)
if [[ $config != *"$new"* ]]; then
    echo "containerd config dump does not name $new" >&2
    exit 1
fi
echo "containerd config dump names $new"

expected_transcribe=$(awk -F '\t' '$4 == "transcribe" && $3 == "running" { print $1 }' "$record/containers.before" | sort)
if [[ -z "$expected_transcribe" ]]; then
    echo 'the survey found no running Transcribe containers' >&2
    exit 1
fi
deadline=$((SECONDS + 300))
while :; do
    running_transcribe=$(docker ps -a --filter label=com.docker.compose.project=transcribe --format '{{.Names}}\t{{.State}}' | awk -F '\t' '$2 == "running" { print $1 }' | sort)
    missing=$(comm -23 <(printf '%s\n' "$expected_transcribe") <(printf '%s\n' "$running_transcribe"))
    if [[ -z "$missing" ]]; then
        break
    fi
    if (( SECONDS >= deadline )); then
        echo "Transcribe containers did not return to running within 300 s: $missing" >&2
        exit 1
    fi
    sleep 5
done
echo 'Transcribe containers running again'

bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.after'
bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.after'
diff -u "$record/images.before" "$record/images.after"
diff -u "$record/containers.before" "$record/containers.after"
unhealthy=$(docker ps --filter health=unhealthy --format '{{.Names}}')
if [[ -n "$unhealthy" ]]; then
    echo "unhealthy containers remain: $unhealthy" >&2
    exit 1
fi
docker system df
du -sb "$new"
df -h / /var/lib/docker
echo 'lists equal'

config=$(containerd --config /etc/containerd/config.toml config dump)
if [[ $config != *"$new"* ]]; then
    echo "containerd config dump no longer names $new; both roots remain" >&2
    exit 1
fi
rm -rf "$old" "$record"
echo 'old root and move record removed'
