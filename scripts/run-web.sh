#!/usr/bin/env bash
# Keeps `refer web` running: started by the "Referral Finder" Windows scheduled task (see README).
# If the site exits, it's started again after 5 seconds, and the time and exit code go to
# .data/web.log (129 = SIGHUP, 137 = SIGKILL, 143 = SIGTERM). If a copy is already running
# (started by hand, or by an earlier run of the task), this just waits.
cd "$(dirname "$0")/.." || exit 1
LOG=.data/web.log
mkdir -p .data

while true; do
    if pgrep -f "[.]venv/bin/refer web" >/dev/null; then
        sleep 10
        continue
    fi
    echo "[$(date '+%F %T')] watchdog: starting refer web" >> "$LOG"
    .venv/bin/refer web >> "$LOG" 2>&1
    echo "[$(date '+%F %T')] watchdog: refer web exited with code $?" >> "$LOG"
    sleep 5
done
