#!/bin/bash
set -euo pipefail
launch_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
conda_cmd="${CONDA_EXE:-}"
if [[ -z "$conda_cmd" || ! -x "$conda_cmd" ]]; then
  conda_cmd="$(command -v conda || true)"
fi
if [[ -z "$conda_cmd" || ! -x "$conda_cmd" ]]; then
  for candidate in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "$HOME/miniforge3/bin/conda" "/opt/homebrew/Caskroom/miniconda/base/bin/conda" "/opt/anaconda3/bin/conda"; do
    if [[ -x "$candidate" ]]; then conda_cmd="$candidate"; break; fi
  done
fi
if [[ -z "$conda_cmd" || ! -x "$conda_cmd" ]]; then
  echo "找不到 conda；請先安裝，或將 CONDA_EXE 設為 conda 執行檔的完整路徑。" >&2
  exit 1
fi
cd -- "$launch_root"
# Activate once, then replace the shell. A long-lived conda run wrapper can
# intercept Ctrl+C before the supervisor has finished closing its children.
eval "$("$conda_cmd" shell.bash hook)"
conda activate tss
# Python checks managed previous sessions and waits for confirmed logout before
# starting any new broker-owning service. It never logs in merely to check usage.
exec python -X utf8 -u "$launch_root/src/service_launcher.py" "$@"
