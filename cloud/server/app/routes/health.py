import os

from fastapi import APIRouter, Response

from app import db

router = APIRouter()

_VERSION = os.environ.get("CLAUSTRUM_VERSION", "dev")


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": _VERSION}


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    try:
        async with db.conn() as c:
            async with c.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
    except Exception as e:
        response.status_code = 503
        return {"ok": False, "error": str(e)}
    return {"ok": True}
