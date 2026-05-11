from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.routes import (
    checkin,
    claim,
    classify,
    health,
    inbox,
    jobs,
    list_peers,
    propose,
    reset,
    resume,
    update,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(
    title="Claustrum Cloud",
    description="Cross-machine coordination for AI-assisted sessions.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(checkin.router, prefix="/v1")
app.include_router(update.router, prefix="/v1")
app.include_router(list_peers.router, prefix="/v1")
app.include_router(claim.router, prefix="/v1")
app.include_router(classify.router, prefix="/v1")
app.include_router(propose.router, prefix="/v1")
app.include_router(resume.router, prefix="/v1")
app.include_router(inbox.router, prefix="/v1")
app.include_router(reset.router, prefix="/v1")
app.include_router(jobs.router, prefix="/jobs")
