from datetime import datetime

from pydantic import BaseModel, Field


class CheckinRequest(BaseModel):
    uid: str
    machine: str
    label: str | None = None
    task: str | None = None
    repo: str | None = None
    branch: str | None = None
    cwd: str | None = None
    is_quiet: bool = False
    is_private: bool = False


class TaxonomyEntry(BaseModel):
    name: str
    description: str


class CheckinResponse(BaseModel):
    ok: bool = True
    topic_required: bool
    taxonomy: list[TaxonomyEntry] | None = None
    first_turn_message: str | None = None


class UpdateRequest(BaseModel):
    uid: str
    task: str | None = None
    files_touched: list[str] | None = None
    pr_number: int | None = None
    last_push_at: datetime | None = None
    status: str | None = Field(default=None, pattern="^(active|paused|done)$")


class ClaimRequest(BaseModel):
    uid: str
    repo: str
    rel_path: str
    ttl_seconds: int = 3600


class ReleaseRequest(BaseModel):
    uid: str
    repo: str
    rel_path: str


class ClassifySelfRequest(BaseModel):
    uid: str
    topic: str
    confidence: int = 80


class ProposeTopicRequest(BaseModel):
    uid: str
    name: str
    description: str


class OkResponse(BaseModel):
    ok: bool = True
