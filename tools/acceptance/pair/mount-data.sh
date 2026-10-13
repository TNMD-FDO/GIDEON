#!/usr/bin/env bash
# The data disk is checked before formatting so an existing filesystem stays intact.
set -euo pipefail

disk=${1:-}
if [[ -z "$disk" || $# -ne 1 ]]; then
    echo 'mount-data.sh requires exactly one data disk' >&2
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

if findmnt -rn -o TARGET --mountpoint /data >/dev/null 2>&1; then
    echo '/data is already mounted; refusing to replace it' >&2
    exit 1
else
    status=$?
    if [[ $status -ne 1 ]]; then
        echo "could not inspect /data mount (exit $status); refusing to format $disk" >&2
        exit 1
    fi
fi

mkfs.ext4 -F "$disk"
install -d /data
uuid=$(blkid -s UUID -o value "$disk")
if [[ -z "$uuid" ]]; then
    echo "could not read the UUID for $disk; refusing to write fstab" >&2
    exit 1
fi
printf 'UUID=%s /data ext4 defaults,nofail 0 2\n' "$uuid" >> /etc/fstab
mount /data
echo "mounted $disk at /data"
