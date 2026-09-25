"""Serve the built SPA, or a one-line hint when it has not been built yet."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

_HINT = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Cat Fleet Chat</title></head>
<body>
<p>Portal assets are not built. From the checkout run <code>npm --prefix web install</code> then <code>npm --prefix web run build</code>, and start the hub again.</p>
</body></html>
"""

_PACKAGE_STATIC = Path(__file__).resolve().parent / "static"
_CHECKOUT_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def dist_dir() -> Path | None:
    """Packaged assets, unless a source checkout has ``web/dist``.

    ``CAT_FLEET_DIST_DIR`` overrides both. An empty value forces the hint page.
    """
    if "CAT_FLEET_DIST_DIR" in os.environ:
        override = os.environ["CAT_FLEET_DIST_DIR"].strip()
        if not override:
            return None
        path = Path(override)
        return path if (path / "index.html").is_file() else None
    # A source checkout's fresh build wins over data copied into the package.
    if ( _CHECKOUT_DIST / "index.html").is_file():
        return _CHECKOUT_DIST
    if (_PACKAGE_STATIC / "index.html").is_file():
        return _PACKAGE_STATIC
    return None


def _safe_file(root: Path, url_path: str) -> Path | None:
    rel = url_path.lstrip("/")
    if not rel:
        rel = "index.html"
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
        return None
    return candidate


async def spa(request: Request):
    """Static file, SPA fallback, or the build hint. ``/api`` and ``/mcp`` stay errors."""
    path = request.url.path
    if path.startswith("/api") or path.startswith("/mcp"):
        return JSONResponse(
            {"error": {"code": "not_found", "message": "Not found"}},
            status_code=404,
        )
    root = dist_dir()
    if root is None:
        if path not in {"/", ""}:
            return HTMLResponse(_HINT, status_code=200)
        return HTMLResponse(_HINT, status_code=200)
    asset = _safe_file(root, path)
    if asset is None:
        asset = root / "index.html"
    media = mimetypes.guess_type(asset.name)[0]
    return FileResponse(asset, media_type=media)


def spa_routes() -> list[Route]:
    return [
        Route("/", spa, methods=["GET"]),
        Route("/{path:path}", spa, methods=["GET"]),
    ]
