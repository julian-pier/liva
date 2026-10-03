#!/usr/bin/env bash
# One-time interactive validation. It creates no new identity and stores no password.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with: sudo $0" >&2; exit 1; }

CONFIG=/etc/liva/recovery/config.toml
mapfile -t DEVICE_CONFIG < <(python3 - "$CONFIG" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as source:
    usb = tomllib.load(source)["usb"]
print(usb["uuid"])
print(usb["label"])
PY
)
UUID=${DEVICE_CONFIG[0]:-}
LABEL=${DEVICE_CONFIG[1]:-}
[[ -n "$UUID" && -n "$LABEL" ]] || { echo "Recovery USB config is incomplete" >&2; exit 1; }
DEVICE=$(blkid -U "$UUID" 2>/dev/null || true)
[[ -b "$DEVICE" ]] || { echo "Configured recovery USB is not present" >&2; exit 1; }
MOUNT=/mnt/liva-recovery
RECIPIENTS=/etc/liva/recovery/recipients.txt
IDENTITY="$MOUNT/backups/full-recovery/bootstrap/liva-recovery-identity.age"
TMP=$(mktemp -d /dev/shm/liva-recovery-validate.XXXXXX)
MOUNTED_HERE=0
cleanup() { rm -rf -- "$TMP"; [[ $MOUNTED_HERE -eq 1 ]] && umount "$MOUNT" || true; }
trap cleanup EXIT INT TERM

[[ "$(blkid -s UUID -o value "$DEVICE")" == "$UUID" ]] || { echo "Unexpected USB UUID" >&2; exit 1; }
[[ "$(lsblk -no LABEL "$DEVICE" | xargs)" == "$LABEL" ]] || { echo "Unexpected USB label" >&2; exit 1; }
install -d -m 0700 "$MOUNT"
if ! findmnt -rn -S "$DEVICE" >/dev/null; then
  mount -o ro,nodev,nosuid,noexec "$DEVICE" "$MOUNT"
  MOUNTED_HERE=1
fi
[[ -f "$IDENTITY" && -f "$RECIPIENTS" ]] || { echo "Recovery artifacts are missing" >&2; exit 1; }

echo "Enter the master password once at the visible age prompt."
age -d "$IDENTITY" > "$TMP/identity.txt"
age-keygen -y "$TMP/identity.txt" > "$TMP/recipient.txt"
cmp -- "$TMP/recipient.txt" "$RECIPIENTS"

printf 'liva-recovery-key-roundtrip\n' > "$TMP/payload.txt"
age -R "$RECIPIENTS" -o "$TMP/payload.age" "$TMP/payload.txt"
age -d -i "$TMP/identity.txt" "$TMP/payload.age" > "$TMP/restored.txt"
cmp -- "$TMP/payload.txt" "$TMP/restored.txt"
printf 'Key validation and synthetic roundtrip succeeded. Public recipient fingerprint: '
sha256sum "$RECIPIENTS" | awk '{print $1}'
