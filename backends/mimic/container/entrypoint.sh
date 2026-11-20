#!/bin/sh
# Dry-run fabric manager: every device command is journalled, nothing is executed. The API token comes from
# FABD_TOKEN (or a generated one, printed once to the log so the console can be given it).
set -e
mkdir -p /etc/osac-fabd /var/lib/osac-fabd
[ -n "${FABD_TOKEN:-}" ] || FABD_TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s' "$FABD_TOKEN" > /etc/osac-fabd/token
touch /etc/osac-fabd/array_key
echo '{"event":"fabd-dry-run","token_set_from_env":'"$([ -n "${FABD_TOKEN_FROM_ENV:-}" ] && echo true || echo false)"'}'
export FABD_CONFIG=/etc/osac-fabd/config.json FABD_DRY_RUN=1 FABD_DRY_LEASE_READS="${FABD_DRY_LEASE_READS:-2}"
# a site config mounted at /config/config.json wins over the built-in mimic one
if [ -s /config/config.json ]; then cp /config/config.json /etc/osac-fabd/config.json; else cp /app/config-dryrun.json /etc/osac-fabd/config.json; fi
exec python3 /app/fabd.py
