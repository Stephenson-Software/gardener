"""The 3D garden page (`/3d`), served by both the dashboard and the hub.

The page is `web/garden3d.html`; it draws through `web/garden3d.js` and
`web/garden3d.wasm`, which `garden3d/build.sh` builds from
`garden3d/main.cpp` against the Strata engine's WebAssembly release, and it
loads and drives them through the Strata web kit's two page scripts,
`web/strata-loader.js` and `web/strata-kit.js` (copied from the vendored kit
in `garden3d/strata-kit/` by the same build). The built files are committed,
so serving them needs nothing beyond the standard library: this module only
maps the page's URLs to those five files.
The page reads the same `/api/status` the 2D dashboard polls, so it needs
no endpoint of its own, and on the hub it sits behind the same operator
check as every other page."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

WEB_DIR = Path(__file__).resolve().parent / "web"

# URL -> (file in WEB_DIR, content type). Nothing outside this table is
# served, so a crafted path can never reach another file.
ASSETS = {
    "/3d": ("garden3d.html", "text/html; charset=utf-8"),
    "/3d/": ("garden3d.html", "text/html; charset=utf-8"),
    "/3d/garden3d.js": ("garden3d.js", "text/javascript; charset=utf-8"),
    "/3d/garden3d.wasm": ("garden3d.wasm", "application/wasm"),
    "/3d/strata-kit.js": ("strata-kit.js", "text/javascript; charset=utf-8"),
    "/3d/strata-loader.js": ("strata-loader.js", "text/javascript; charset=utf-8"),
}


def is_asset_path(path: str) -> bool:
    return path in ASSETS


def asset(path: str) -> Optional[tuple[str, bytes]]:
    """(content type, body) for one of the page's URLs, or None when the
    path isn't one of them or the file is missing from this install."""
    entry = ASSETS.get(path)
    if entry is None:
        return None
    name, content_type = entry
    try:
        return content_type, (WEB_DIR / name).read_bytes()
    except OSError:
        return None
