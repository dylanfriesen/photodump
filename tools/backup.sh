#!/usr/bin/env bash
# Nightly photodump snapshot. Installed in dylan's crontab:
#
#   15 6 * * * /home/dylan/photodump/tools/backup.sh
#
# The work happens inside the live container (see app/backup.py for why).
# Output goes to data/backups/backup.log. If the container is down, the failure
# goes to ~/photodump-backup-failures.log instead, so a stopped container is
# noticed rather than silently skipping every night.
set -uo pipefail
cd "$(dirname "$0")/.."
log=data/backups/backup.log

if ! out=$(docker exec photodump python -m app.backup 2>&1); then
  msg="$(date '+%F %T') BACKUP FAILED: ${out//$'\n'/ | }"
  # data/ is root-owned; write through the container when it is up, else to
  # the invoking user's home so the failure is still recorded somewhere.
  docker exec photodump sh -c "mkdir -p /srv/data/backups && echo \"\$1\" >> /srv/$log" _ "$msg" 2>/dev/null \
    || echo "$msg" >> "$HOME/photodump-backup-failures.log"
  exit 1
fi
docker exec photodump sh -c "echo \"\$1\" >> /srv/$log" _ "$out"
