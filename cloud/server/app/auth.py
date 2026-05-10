import os

from fastapi import Header, HTTPException


def _header_name() -> str:
    return os.environ.get("CLAUSTRUM_AUTH_HEADER", "X-Claustrum-User-Email")


async def current_user(
    x_claustrum_user_email: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: return the authenticated caller's email.

    Trusts the upstream proxy (IAP, Cloudflare Access, Tailscale + Authelia,
    Caddy, etc.) to set the header. The header name is configurable via
    CLAUSTRUM_AUTH_HEADER for operators whose proxies emit a different name.
    """
    if not x_claustrum_user_email:
        raise HTTPException(
            status_code=401,
            detail=f"Missing {_header_name()} header — server must sit behind an authenticated proxy.",
        )
    return x_claustrum_user_email.strip().lower()
