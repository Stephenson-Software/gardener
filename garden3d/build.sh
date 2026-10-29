#!/usr/bin/env bash
# Builds the 3D garden (garden3d/main.cpp) into gardener/web/garden3d.{js,wasm},
# and copies the Strata web kit's page scripts (strata-kit.js,
# strata-loader.js) beside them: the four files the dashboard and the hub
# serve at /3d/. All are committed, so gardener itself stays stdlib-only
# Python with no build step; CI reruns this and fails if the committed files
# differ from what the source builds.
#
# The shell around the scene is the Strata web kit (docs/WEB.md in Strata,
# "The web kit"), vendored once in strata-kit/ at the repository's root (Fleet
# City builds on the same copy) at the Strata commit in strata-kit/PIN until a
# Strata release carries it; strata-kit/update.sh re-vendors it. The engine is
# Strata's v0.7.0 wasm release, checked against a pinned SHA-256; the kit uses
# only its C ABI. Needs emsdk 6.0.11 activated (the version Strata's release is
# built with: everything linked into one .wasm must agree on it and on
# -fwasm-exceptions).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
kit="$repo/strata-kit"

# The vendored kit must be exactly what PIN names (update.sh writes both).
(cd "$kit" && sha256sum -c --quiet SHA256SUMS)

export KIT_SOURCE="$here/main.cpp"
export KIT_OUT="$repo/gardener/web"
export KIT_NAME=garden3d
export KIT_EXPORT_NAME=createGarden3D
export KIT_EXPORTS=_g3_set_beds,_g3_set_plants,_g3_set_gardeners,_g3_set_shed
export KIT_WORK="${GARDEN3D_WORK:-$here/.build}"
export EMSDK_VERSION=6.0.11
export STRATA_VERSION=0.7.0
export STRATA_SHA256=1aab559eb2477f42bfbeae1cd2e3cf70fcb8aa8b28fc8dce16ba1e0ba855049f

"$kit/build-kit.sh"
