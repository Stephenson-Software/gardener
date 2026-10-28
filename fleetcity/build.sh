#!/usr/bin/env bash
# Builds Fleet City (fleetcity/main.cpp) into gardener/web/city.{js,wasm},
# the two files the hub serves at /city/. Both are committed, so gardener
# itself stays stdlib-only Python with no build step; CI reruns this and
# fails if the committed files differ from what the source builds.
#
# Needs emsdk 6.0.11 activated (the version Strata's release is built with:
# everything linked into one .wasm must agree on it and on -fwasm-exceptions).
# Strata comes from its v0.6.0 release, checked against a pinned SHA-256.
set -euo pipefail

STRATA_VERSION=0.6.0
STRATA_SHA256=9973a0d0850abb0bfc58c20497b500a3d471033d6e04f73fcdb41382221b3016
EMSDK_VERSION=6.0.11

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
work="${FLEETCITY_WORK:-$here/.build}"
mkdir -p "$work"

have="$(emcc --version | head -1 | sed -E 's/.* ([0-9]+\.[0-9]+\.[0-9]+) .*/\1/')"
if [ "$have" != "$EMSDK_VERSION" ]; then
  echo "fleetcity: emcc is $have; activate emsdk $EMSDK_VERSION first" >&2
  exit 1
fi

tarball="$work/strata-$STRATA_VERSION-wasm32-emscripten.tar.gz"
if [ ! -f "$tarball" ]; then
  curl -fsSL -o "$tarball.part" \
    "https://github.com/Preponderous-Software/strata/releases/download/v$STRATA_VERSION/strata-$STRATA_VERSION-wasm32-emscripten.tar.gz"
  mv "$tarball.part" "$tarball"
fi
echo "$STRATA_SHA256  $tarball" | sha256sum -c --quiet -
rm -rf "$work/strata" && mkdir -p "$work/strata"
tar xzf "$tarball" -C "$work/strata"

em++ "$here/main.cpp" "$work/strata/lib/libstrata.a" -I"$work/strata/include" \
  -std=c++17 -O2 -fwasm-exceptions -Wall -Wextra \
  -sMIN_WEBGL_VERSION=2 -sMAX_WEBGL_VERSION=2 -sALLOW_MEMORY_GROWTH=1 \
  -sENVIRONMENT=web -sMODULARIZE=1 -sEXPORT_NAME=createFleetCity \
  -sEXPORTED_FUNCTIONS=_malloc,_free,_fc_init,_fc_resize,_fc_set_quality,_fc_set_city,_fc_set_palette,_fc_set_beacons,_fc_set_glows,_fc_set_selected,_fc_set_camera,_fc_set_sun,_fc_view_proj,_fc_pick,_fc_frame \
  -sEXPORTED_RUNTIME_METHODS=HEAPF32,HEAPU8,stringToNewUTF8 \
  -o "$work/city.js"

cp "$work/city.js" "$work/city.wasm" "$repo/gardener/web/"
ls -l "$repo/gardener/web/city.js" "$repo/gardener/web/city.wasm"
