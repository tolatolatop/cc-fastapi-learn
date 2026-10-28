#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  chown -R ccagent:ccagent /app/data
  chown ccagent:ccagent /home/ccagent/.claude
  exec gosu ccagent "$@"
fi

exec "$@"
