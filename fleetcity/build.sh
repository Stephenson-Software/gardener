#!/usr/bin/env bash
# Builds Fleet City (fleetcity/main.cpp) into gardener/web/city.{js,wasm}, and
# copies the Strata web kit's page scripts (strata-kit.js, strata-loader.js)
# beside them: the hub serves all four at /city/. All are committed, so
# gardener itself stays stdlib-only Python with no build step; CI reruns this
# and fails if the committed files differ from what the source builds.
#
# The shell around the city is the Strata web kit (docs/WEB.md in Strata,
# "The web kit"), the same vendored copy the 3D garden builds on:
# strata-kit/ at the repository's root, at the Strata commit in
# strata-kit/PIN. The two kit scripts are shared with /3d, so this build and
# garden3d/build.sh write the same bytes to gardener/web/. The engine is
# Strata's v0.6.0 wasm release, checked against a pinned SHA-256; the kit
# uses only its C ABI. Needs emsdk 6.0.11 activated (the version Strata's
# release is built with: everything linked into one .wasm must agree on it
# and on -fwasm-exceptions).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
kit="$repo/strata-kit"

# The vendored kit must be exactly what PIN names (update.sh writes both).
(cd "$kit" && sha256sum -c --quiet SHA256SUMS)

export KIT_SOURCE="$here/main.cpp"
export KIT_OUT="$repo/gardener/web"
export KIT_NAME=city
export KIT_EXPORT_NAME=createFleetCity
export KIT_EXPORTS=_fc_set_city,_fc_set_palette,_fc_set_beacons,_fc_set_glows,_fc_set_selected,_fc_pick
export KIT_WORK="${FLEETCITY_WORK:-$here/.build}"
export EMSDK_VERSION=6.0.11
export STRATA_VERSION=0.6.0
export STRATA_SHA256=9973a0d0850abb0bfc58c20497b500a3d471033d6e04f73fcdb41382221b3016

"$kit/build-kit.sh"
