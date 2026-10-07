#!/usr/bin/env bash
set -euo pipefail
energyhub_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$(dirname -- "$energyhub_dir")${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# Discover a local Flask interpreter; never install or mutate its environment.
energyhub_launcher_python="${ENERGYHUB_API_PYTHON:-python3}"
energyhub_web_python="$("$energyhub_launcher_python" -c 'from Energyhub.runtime import api_python_executable; print(api_python_executable())')"
exec "$energyhub_web_python" -m Energyhub.app "$@"
