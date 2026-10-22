#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "${LIVE_SUMMARY_COMPOSE_HELPER:-0}" != 1 ] && docker compose version >/dev/null 2>&1; then
  exec docker compose "$@"
fi
# Only this ephemeral CLI helper has the Docker socket, never the applications.
# Keep the same absolute project path for daemon-side bind mounts.
tty_flag=''
if [ -t 0 ] && [ -t 1 ]; then tty_flag='-t'; fi
exec docker run --rm -i $tty_flag \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:$PWD" -w "$PWD" \
  docker:27.5.1-cli compose "$@"
