#!/usr/bin/env bash
# Synthetic co-tenant for the acceptance VM. It represents no real application
# and follows the box ledger's rules when arriving before or after the product.
set -euo pipefail

fixture_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
verb=${1:-}

case "$verb" in
    first)
        disk=${2:?first requires the data disk}
        if [[ $# -ne 2 ]]; then
            echo 'first requires exactly one data disk' >&2
            exit 1
        fi
        if blkid -p "$disk" >/dev/null 2>&1; then
            echo "$disk already has a filesystem; refusing to format it" >&2
            exit 1
        else
            status=$?
            if [[ $status -ne 2 ]]; then
                echo "could not inspect $disk; refusing to format it" >&2
                exit 1
            fi
        fi
        if findmnt -rn --mountpoint /data >/dev/null; then
            echo '/data is already mounted; refusing to replace it' >&2
            exit 1
        fi
        mkfs.ext4 -F "$disk"
        install -d /data
        uuid=$(blkid -s UUID -o value "$disk")
        printf 'UUID=%s /data ext4 defaults,nofail 0 2\n' "$uuid" >> /etc/fstab
        mount /data
        echo "mounted $disk at /data"

        . /etc/os-release
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod 0644 /etc/apt/keyrings/docker.asc
        cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        echo 'installed Docker from docker.sources'

        install -d /etc/containerd /var/lib/docker/containerd /etc/docker
        containerd config default > /etc/containerd/config.toml
        python3 - /etc/containerd/config.toml <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
text, count = re.subn(r'(?m)^root\s*=\s*[^\n]+$', "root = '/var/lib/docker/containerd'", text, count=1)
if count != 1:
    raise SystemExit('containerd default config has no root setting')
path.write_text(text)
PY
        printf '{"max-concurrent-downloads": 3}\n' > /etc/docker/daemon.json
        systemctl restart containerd docker
        echo 'set containerd root and Docker daemon key'
        ;;
    later)
        if [[ $# -ne 1 ]]; then
            echo 'later takes no data disk' >&2
            exit 1
        fi
        if ! docker --version; then
            echo 'Docker is missing' >&2
            exit 1
        fi
        if ! systemctl is-active --quiet docker.service; then
            echo 'docker.service is not active' >&2
            exit 1
        fi
        python3 - /etc/docker/daemon.json <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
settings = json.loads(path.read_text())
if not isinstance(settings, dict):
    raise SystemExit('Docker daemon settings are not an object')
settings['max-concurrent-downloads'] = 3
with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
    json.dump(settings, stream, indent=2)
    stream.write('\n')
    temporary = stream.name
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
        systemctl reload docker
        ready=false
        for _ in $(seq 1 30); do
            if docker info >/dev/null 2>&1; then
                ready=true
                break
            fi
            sleep 1
        done
        if [[ $ready != true ]]; then
            echo 'Docker did not answer after reload' >&2
            exit 1
        fi
        echo 'merged Docker daemon key and reloaded Docker'
        ;;
    *)
        echo 'usage: arrive.sh {first <data-disk>|later}' >&2
        exit 1
        ;;
esac

DEBIAN_FRONTEND=noninteractive apt-get install -y busybox-static
# The image's root holds the one static binary and nothing else.
rootfs=$(mktemp -d)
install -D -m 0755 /bin/busybox "$rootfs/bin/busybox"
tar -C "$rootfs" -cf - . | docker import -c 'CMD ["/bin/busybox", "sleep", "infinity"]' - cotenant:local
rm -rf -- "$rootfs"
echo 'imported cotenant:local'
install -d /data/cotenant /opt/cotenant
printf 'cotenant data\n' > /data/cotenant/marker
install -m 0644 "$fixture_dir/compose.yaml" /opt/cotenant/compose.yaml
docker compose -f /opt/cotenant/compose.yaml up -d
echo 'started cotenant-app with /data/cotenant/marker'
install -m 0644 "$fixture_dir/cotenant-rule.service" /etc/systemd/system/cotenant-rule.service
install -m 0644 "$fixture_dir/cotenant-tick.service" /etc/systemd/system/cotenant-tick.service
install -m 0644 "$fixture_dir/cotenant-tick.timer" /etc/systemd/system/cotenant-tick.timer
systemctl daemon-reload
systemctl enable --now cotenant-rule.service
systemctl enable --now cotenant-tick.timer
echo 'enabled cotenant-rule.service and cotenant-tick.timer'
