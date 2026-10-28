#!/usr/bin/env bash
# build-kit.sh: builds a Strata web-kit scene (one C++ file that includes
# strata_kit.h) into <name>.js + <name>.wasm, and copies the kit's page
# scripts (strata-kit.js, strata-loader.js) beside them.
#
# Part of the Strata web kit (docs/WEB.md, "The web kit"). Settings come from
# the environment, or from a shell file given as the first argument (it is
# sourced, so it can set the same names):
#
#   KIT_SOURCE       the scene's .cpp (required)
#   KIT_OUT          where the outputs go (required)
#   KIT_NAME         output basename: KIT_OUT/KIT_NAME.{js,wasm} (default: scene)
#   KIT_EXPORT_NAME  the module factory's name, -sEXPORT_NAME (default: createScene)
#   KIT_EXPORTS      the scene's own exports, comma-separated, each with its
#                    leading underscore (e.g. _g3_set_beds,_g3_set_plants)
#   KIT_COPY_JS      1 (default) copies strata-kit.js and strata-loader.js to KIT_OUT
#   KIT_PAGES        more files to copy to KIT_OUT (space-separated), e.g. the page
#   KIT_DIR          the kit (default: this script's directory)
#   KIT_WORK         scratch for downloads (default: $KIT_OUT/../.strata-kit-build)
#   EMSDK_VERSION    the emcc that must be active (default 6.0.11, Strata's)
#
# Strata itself, one of:
#   STRATA_VERSION + STRATA_SHA256   a release's wasm32-emscripten tarball,
#                                    downloaded once and checked
#   STRATA_TARBALL + STRATA_SHA256   that tarball from a local path
#   STRATA_LIB + STRATA_INCLUDE      a libstrata.a and the directory holding
#                                    strata/strata.h (a source build)
#
# The flags are fixed so the same inputs build the same bytes: a host can
# commit the outputs and have CI rebuild and `git diff --exit-code` them.
set -euo pipefail

if [ $# -ge 1 ]; then
  # shellcheck disable=SC1090
  source "$1"
fi

KIT_DIR="${KIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
: "${KIT_SOURCE:?KIT_SOURCE (the scene .cpp) is required}"
: "${KIT_OUT:?KIT_OUT (the output directory) is required}"
KIT_NAME="${KIT_NAME:-scene}"
KIT_EXPORT_NAME="${KIT_EXPORT_NAME:-createScene}"
KIT_EXPORTS="${KIT_EXPORTS:-}"
KIT_COPY_JS="${KIT_COPY_JS:-1}"
EMSDK_VERSION="${EMSDK_VERSION:-6.0.11}"

mkdir -p "$KIT_OUT"
KIT_OUT="$(cd "$KIT_OUT" && pwd)"
KIT_WORK="${KIT_WORK:-$(dirname "$KIT_OUT")/.strata-kit-build}"
mkdir -p "$KIT_WORK"

have="$(emcc --version | head -1 | sed -E 's/.* ([0-9]+\.[0-9]+\.[0-9]+) .*/\1/')"
if [ "$have" != "$EMSDK_VERSION" ]; then
  echo "build-kit: emcc is $have; activate emsdk $EMSDK_VERSION first" >&2
  exit 1
fi

if [ -z "${STRATA_LIB:-}" ]; then
  if [ -z "${STRATA_TARBALL:-}" ]; then
    : "${STRATA_VERSION:?set STRATA_VERSION + STRATA_SHA256, STRATA_TARBALL + STRATA_SHA256, or STRATA_LIB + STRATA_INCLUDE}"
    STRATA_TARBALL="$KIT_WORK/strata-$STRATA_VERSION-wasm32-emscripten.tar.gz"
    if [ ! -f "$STRATA_TARBALL" ]; then
      curl -fsSL -o "$STRATA_TARBALL.part" \
        "https://github.com/Preponderous-Software/strata/releases/download/v$STRATA_VERSION/strata-$STRATA_VERSION-wasm32-emscripten.tar.gz"
      mv "$STRATA_TARBALL.part" "$STRATA_TARBALL"
    fi
  fi
  : "${STRATA_SHA256:?STRATA_SHA256 is required with a tarball}"
  echo "$STRATA_SHA256  $STRATA_TARBALL" | sha256sum -c --quiet -
  rm -rf "$KIT_WORK/strata" && mkdir -p "$KIT_WORK/strata"
  tar xzf "$STRATA_TARBALL" -C "$KIT_WORK/strata"
  STRATA_LIB="$KIT_WORK/strata/lib/libstrata.a"
  STRATA_INCLUDE="$KIT_WORK/strata/include"
fi
: "${STRATA_INCLUDE:?STRATA_INCLUDE is required with STRATA_LIB}"

exports="_malloc,_free,_sk_init,_sk_restore,_sk_kit_version,_sk_resize,_sk_set_quality,_sk_set_camera,_sk_set_sun,_sk_view_proj,_sk_stats,_sk_frame"
if [ -n "$KIT_EXPORTS" ]; then exports="$exports,$KIT_EXPORTS"; fi

em++ "$KIT_SOURCE" "$STRATA_LIB" -I"$STRATA_INCLUDE" -I"$KIT_DIR" \
  -std=c++17 -O2 -fwasm-exceptions -Wall -Wextra \
  -sMIN_WEBGL_VERSION=2 -sMAX_WEBGL_VERSION=2 -sALLOW_MEMORY_GROWTH=1 \
  -sENVIRONMENT=web -sMODULARIZE=1 -sEXPORT_NAME="$KIT_EXPORT_NAME" \
  -sEXPORTED_FUNCTIONS="$exports" \
  -sEXPORTED_RUNTIME_METHODS=HEAPF32,HEAP32,stringToNewUTF8,UTF8ToString \
  -o "$KIT_WORK/$KIT_NAME.js"

cp "$KIT_WORK/$KIT_NAME.js" "$KIT_WORK/$KIT_NAME.wasm" "$KIT_OUT/"
if [ "$KIT_COPY_JS" = "1" ]; then
  cp "$KIT_DIR/strata-kit.js" "$KIT_DIR/strata-loader.js" "$KIT_OUT/"
fi
for page in ${KIT_PAGES:-}; do cp "$page" "$KIT_OUT/"; done
ls -l "$KIT_OUT/$KIT_NAME.js" "$KIT_OUT/$KIT_NAME.wasm"
