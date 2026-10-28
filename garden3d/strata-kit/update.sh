#!/usr/bin/env bash
# Re-vendors the Strata web kit (Strata's web/kit/) at one commit:
#   garden3d/strata-kit/update.sh <full commit sha>
# then rerun garden3d/build.sh and commit the kit, PIN and the rebuilt files.
# Once a Strata release carries the kit (its wasm tarball has kit/), this
# directory can go and build.sh can take the kit from the tarball instead.
set -euo pipefail
commit="${1:?usage: update.sh <full strata commit sha>}"
case "$commit" in *[!0-9a-f]* | "") echo "update.sh: give a full hex commit sha" >&2; exit 1 ;; esac
[ "${#commit}" -eq 40 ] || { echo "update.sh: give the full 40-character sha" >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
base="https://raw.githubusercontent.com/Preponderous-Software/strata/$commit"
for f in strata_kit.h strata-kit.js strata-loader.js build-kit.sh; do
  curl -fsSL -o "$here/$f" "$base/web/kit/$f"
done
curl -fsSL -o "$here/LICENSE" "$base/LICENSE"
chmod +x "$here/build-kit.sh"
echo "$commit" > "$here/PIN"
(cd "$here" && sha256sum strata_kit.h strata-kit.js strata-loader.js build-kit.sh LICENSE > SHA256SUMS)
echo "vendored the Strata web kit at $commit"
