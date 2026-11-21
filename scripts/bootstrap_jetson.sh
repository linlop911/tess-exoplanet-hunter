#!/usr/bin/env bash
set -euo pipefail

root=/home/data/astro_tess
prefix=/home/data/miniforge3
export TMPDIR="$root/tmp"
export XDG_CACHE_HOME="$root/cache"
export CONDA_PKGS_DIRS="$root/cache/conda-pkgs"
export CONDARC="$root/condarc"
export MPLCONFIGDIR="$root/cache/matplotlib"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR" "$CONDA_PKGS_DIRS" "$MPLCONFIGDIR" "$root/logs" "$root/golden" "$root/raw" "$root/out" "$root/scripts"
printf 'channels:\n  - conda-forge\nchannel_priority: strict\n' > "$CONDARC"

if [[ ! -x "$prefix/bin/conda" ]]; then
    asset=Miniforge3-Linux-aarch64.sh
    metadata="$root/logs/miniforge-release.json"
    curl -fsSL --retry 2 --max-time 30 https://api.github.com/repos/conda-forge/miniforge/releases/latest -o "$metadata"
    asset_info=$(/usr/bin/python3 - "$metadata" "$asset" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as source:
    release = json.load(source)
asset = next(item for item in release['assets'] if item['name'] == sys.argv[2])
assert asset['digest'].startswith('sha256:')
print(asset['browser_download_url'], asset['digest'].split(':', 1)[1])
PY
    )
    read -r url digest <<< "$asset_info"
    printf '%s\nSHA-256 %s\n' "$url" "$digest" > "$root/logs/miniforge-source.txt"
    [[ "$digest" =~ ^[[:xdigit:]]{64}$ ]]
    if ! printf '%s  %s\n' "$digest" "$TMPDIR/$asset" | sha256sum -c --status; then
        curl -fsSL --retry 2 --max-time 240 --max-filesize 150000000 "$url" -o "$TMPDIR/$asset"
    fi
    printf '%s  %s\n' "$digest" "$TMPDIR/$asset" | sha256sum -c -
    bash "$TMPDIR/$asset" -b -p "$prefix"
fi

conda="$prefix/bin/conda"
env_prefix="$prefix/envs/astro"
if [[ ! -x "$env_prefix/bin/python" ]]; then
    "$conda" create -y -p "$env_prefix" python=3.11 numpy scipy pandas matplotlib-base astropy astroquery lightkurve
fi
"$env_prefix/bin/python" -c 'import astropy, matplotlib, numpy, scipy; print(astropy.__version__, matplotlib.__version__, numpy.__version__, scipy.__version__)'
"$conda" list -p "$env_prefix" --explicit > "$root/logs/astro-conda-explicit.txt"
df -BG / /home/data
