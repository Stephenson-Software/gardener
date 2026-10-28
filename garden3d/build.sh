#!/usr/bin/env bash
# Builds the 3D garden (garden3d/main.cpp) into gardener/web/garden3d.{js,wasm},
# the two files the dashboard and the hub serve at /3d/. Both are committed,
# so gardener itself stays stdlib-only Python with no build step; CI reruns
# this and fails if the committed files differ from what the source builds.
#
# Needs emsdk 6.0.11 activated (the version Strata's release is built with:
# everything linked into one .wasm must agree on it and on -fwasm-exceptions).
# Strata comes from its v0.4.0 release, checked against a pinned SHA-256.
set -euo pipefail

STRATA_VERSION=0.4.0
STRATA_SHA256=530811965a98f40693c5c29a5a2b24ea52a58c71d31a89a0e5dc795629f99b7e
EMSDK_VERSION=6.0.11

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
work="${GARDEN3D_WORK:-$here/.build}"
mkdir -p "$work"

have="$(emcc --version | head -1 | sed -E 's/.* ([0-9]+\.[0-9]+\.[0-9]+) .*/\1/')"
if [ "$have" != "$EMSDK_VERSION" ]; then
  echo "garden3d: emcc is $have; activate emsdk $EMSDK_VERSION first" >&2
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
  -sENVIRONMENT=web -sMODULARIZE=1 -sEXPORT_NAME=createGarden3D \
  -sEXPORTED_FUNCTIONS=_malloc,_free,_g3_init,_g3_resize,_g3_set_quality,_g3_set_beds,_g3_set_plants,_g3_set_gardeners,_g3_set_shed,_g3_set_camera,_g3_set_sun,_g3_view_proj,_g3_frame \
  -sEXPORTED_RUNTIME_METHODS=HEAPF32,stringToNewUTF8 \
  -o "$work/garden3d.js"

cp "$work/garden3d.js" "$work/garden3d.wasm" "$repo/gardener/web/"
ls -l "$repo/gardener/web/garden3d.js" "$repo/gardener/web/garden3d.wasm"
