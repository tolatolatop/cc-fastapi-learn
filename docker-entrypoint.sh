#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  chown -R ccagent:ccagent /app/data
  exec gosu ccagent "$@"
fi

exec "$@"
