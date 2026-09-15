#!/usr/bin/env bash
# Install the 03:00 wake-and-drain task on the desktop render node, over SSH.
#
#   ./tools/install-night-drain.sh           # now; fails if the desktop is asleep
#   ./tools/install-night-drain.sh --auto    # from cron: quiet no-op while the
#                                            # desktop is unreachable, removes its
#                                            # own crontab line once installed
#
# The desktop sleeps most of the time and kanto cannot wake it, so a one-off
# install has to wait for it to be on. --auto is that wait. Uninstall with:
#   ssh desktop 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\ComfyUI\uninstall-night-drain.ps1'
set -uo pipefail
cd "$(dirname "$0")/.."

AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1
LOG="$HOME/.photodump-night-drain-install.log"
say() { echo "$(date '+%F %T') $*"; }

if ! ssh -o ConnectTimeout=6 -o BatchMode=yes desktop 'exit' >/dev/null 2>&1; then
  [ $AUTO = 1 ] && exit 0
  say "desktop unreachable over SSH (asleep?); try again when it is on, or use --auto from cron"
  exit 1
fi

say "desktop reachable; installing night drain"
scp -q -o BatchMode=yes desktop/night-drain.ps1 desktop/install-night-drain.ps1 \
  desktop/uninstall-night-drain.ps1 desktop:C:/ComfyUI/ || { say "copy failed"; exit 1; }

out=$(ssh -o BatchMode=yes desktop \
  'powershell -NoProfile -ExecutionPolicy Bypass -File C:\ComfyUI\install-night-drain.ps1' 2>&1)
status=$?
echo "$out"

if [ $status -eq 0 ] && grep -q 'wake to run: *True' <<<"$out"; then
  say "installed"
  if [ $AUTO = 1 ]; then
    crontab -l 2>/dev/null | grep -v 'tools/install-night-drain.sh' | crontab -
    say "removed the --auto crontab entry"
  fi
  exit 0
fi
say "install did not confirm (exit $status); the --auto entry stays and will retry"
exit 1
