#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [ "$#" -eq 0 ]; then
    printf '%s\n' 'Specify --config /absolute/path/to/your-confirmed-wiring.json.' >&2
    exit 2
fi
exec python3 -m manual_station "$@" --backend real
