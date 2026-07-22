#!/bin/sh
set -eu

export PYTHONPATH="/app/src${PYTHONPATH:+:$PYTHONPATH}"

# When invoked with `docker exec`, connect to the API in this same container.
# Explicit CC_FASTAPI_* values still take precedence for standalone use.
: "${CC_FASTAPI_BASE_URL:=http://127.0.0.1:8000}"
export CC_FASTAPI_BASE_URL
if [ -z "${CC_FASTAPI_TOKEN:-}" ] && [ -n "${API_TOKEN:-}" ]; then
    CC_FASTAPI_TOKEN="$API_TOKEN"
    export CC_FASTAPI_TOKEN
fi

exec python -m cc_fastapi.cli "$@"
