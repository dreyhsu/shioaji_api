#!/bin/bash
launch_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$launch_root/start_all.sh" "$@"
