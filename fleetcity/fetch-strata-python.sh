#!/usr/bin/env bash
# Fetches what `gardener hub city-still` draws with: Strata's released
# Linux library and its pure-ctypes Python binding, both pinned by SHA-256.
# The binding is not a release asset, so its four files come from the
# v0.6.0 tag's commit. Nothing is installed; point the still at the result:
#
#   fleetcity/fetch-strata-python.sh /opt/strata
#   GARDENER_STRATA_LIB=/opt/strata/libstrata-0.6.0-linux-x64.so \
#   GARDENER_STRATA_PYTHON=/opt/strata/python gardener hub city-still --out still.png
#
# Rendering needs Mesa's EGL (llvmpipe is enough): libegl1 libgl1-mesa-dri.
set -euo pipefail

dest="${1:?usage: fetch-strata-python.sh <dir>}"
VERSION=0.6.0
COMMIT=c2d7ee4c1102726c654c90d8361383d9ba3590b3   # tag v0.6.0
LIB_SHA256=3815068f90afa67b42177359a7ea918a5de321d7f7bad8706fa5275c4906af2a
FILES="
7d9ff9dad14fdf4aabd3a108c7d571465230947b91978dcef8fea8d9782d2d56 pyproject.toml
86381523746aa9bf5c4a71ca734fd316972d67d1037482ed4af3bdfb8c4863cb strata/__init__.py
372f03171126262068962e3fa83d70a91a8353603473f59e8df0fe13a671a094 strata/_abi.py
8b2e47f33ae815f6a73d6a1a944d70b17feceeaa9b544d4711646e0b302283db strata/_loader.py
"

mkdir -p "$dest/python/strata"
fetch() {  # url sha256 path
  curl -fsSL -o "$3.part" "$1"
  echo "$2  $3.part" | sha256sum -c --quiet -
  mv "$3.part" "$3"
}
lib="$dest/libstrata-$VERSION-linux-x64.so"
[ -f "$lib" ] && echo "$LIB_SHA256  $lib" | sha256sum -c --quiet - 2>/dev/null || \
  fetch "https://github.com/Preponderous-Software/strata/releases/download/v$VERSION/libstrata-$VERSION-linux-x64.so" \
    "$LIB_SHA256" "$lib"
echo "$FILES" | while read -r sha path; do
  [ -n "$sha" ] || continue
  fetch "https://raw.githubusercontent.com/Preponderous-Software/strata/$COMMIT/bindings/python/$path" \
    "$sha" "$dest/python/$path"
done
echo "fetched libstrata $VERSION and its Python binding into $dest"
