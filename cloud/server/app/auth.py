import os

from fastapi import HTTPException, Request


def _header_name() -> str:
    return os.environ.get("CLAUSTRUM_AUTH_HEADER", "X-Claustrum-User-Email")


def _strip_iap_prefix(value: str) -> str:
    """IAP prepends `accounts.google.com:` to the email in
    X-Goog-Authenticated-User-Email. Strip it so downstream code sees a
    plain email. Other proxies (Cloudflare Access, Authelia, Tailscale)
    send the email unprefixed — this is a no-op for them."""
    prefix = "accounts.google.com:"
    return value[len(prefix):] if value.startswith(prefix) else value


async def current_user(request: Request) -> str:
    """FastAPI dependency: return the authenticated caller's email.

    Trusts the upstream proxy (IAP, Cloudflare Access, Tailscale + Authelia,
    Caddy, etc.) to set the header. The header name is configurable via
    CLAUSTRUM_AUTH_HEADER — we read it at request time so a single deployment
    can switch proxies without a code change.
    """
    header = _header_name()
    value = request.headers.get(header)
    if not value:
        raise HTTPException(
            status_code=401,
            detail=f"Missing {header} header — server must sit behind an authenticated proxy.",
        )
    return _strip_iap_prefix(value).strip().lower()
