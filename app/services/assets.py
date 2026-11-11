"""Content-addressed URLs and service-worker metadata for public static assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
ASSET_VERSION_LENGTH = 12

# Only public static assets are cached; authenticated pages and financial data are not.
PUBLIC_ASSET_PATHS = (
    "/static/css/app.css",
    "/static/css/palette-v5.css",
    "/static/css/shell-v5.css",
    "/static/css/product-v5.css",
    "/static/css/daily-v5.css",
    "/static/css/chart-tooltips.css",
    "/static/css/finance-visuals.css",
    "/static/js/app.js",
    "/static/js/context-v5.js",
    "/static/js/chart-tooltips.js",
    "/static/js/card-billing.js",
    "/static/icons/app-icon.svg",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/apple-touch-icon.png",
    "/static/manifest.webmanifest",
)


def _asset_file(path: str) -> tuple[str, Path]:
    """Return a canonical public path and its safe file under ``STATIC_ROOT``."""
    parsed = urlsplit(str(path))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/static/"):
        raise ValueError(f"Asset path must be local and start with /static/: {path!r}")

    relative = unquote(parsed.path[len("/static/"):])
    candidate = (STATIC_ROOT / relative).resolve()
    static_root = STATIC_ROOT.resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError as exc:
        raise ValueError(f"Asset path escapes the static directory: {path!r}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return f"/static/{relative}", candidate


def asset_url(path: str) -> str:
    """Return a public static URL whose query version is a short content hash.

    Existing query parameters are intentionally discarded so old fixed versions
    cannot survive when a template is updated.
    """
    canonical_path, file_path = _asset_file(path)
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:ASSET_VERSION_LENGTH]
    return f"{canonical_path}?v={digest}"


def public_assets() -> list[str]:
    """Return the fingerprinted URLs that make up the cacheable public shell."""
    return [asset_url(path) for path in PUBLIC_ASSET_PATHS]


# A descriptive alias is useful to callers that prefer the URL terminology.
public_asset_urls = public_assets


def shell_cache_name(asset_urls: Iterable[str] | None = None) -> str:
    """Return a cache name that changes whenever a public asset URL changes."""
    urls = tuple(public_assets() if asset_urls is None else asset_urls)
    digest = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()[:ASSET_VERSION_LENGTH]
    return f"finfam-shell-{digest}"


def service_worker_source() -> str:
    """Inject current asset URLs and cache digest into the SW source template."""
    template_path = STATIC_ROOT / "js" / "sw.js"
    template = template_path.read_text(encoding="utf-8")
    return (
        template
        .replace("__FINFAM_CACHE_NAME__", shell_cache_name())
        .replace(
            "__FINFAM_PUBLIC_ASSETS__",
            json.dumps(public_assets(), ensure_ascii=False, separators=(",", ":")),
        )
    )
