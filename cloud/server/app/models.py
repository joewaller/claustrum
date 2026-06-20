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
    # When the session is already tagged, echo its topic back so the client can
    # mirror it down to the local state DB (offline-joinable by uid). None while
    # untagged (the client then auto-classifies from `taxonomy`).
    topic: str | None = None
    topic_confidence: int | None = None
    taxonomy: list[TaxonomyEntry] | None = None
    first_turn_message: str | None = None


class UpdateRequest(BaseModel):
    uid: str
    task: str | None = None
    working_on: str | None = None
    files_touched: list[str] | None = None
    pr_number: int | None = None
    last_push_at: datetime | None = None
    status: str | None = Field(default=None, pattern="^(active|paused|done)$")
    # Value-scrubbed summary of how the work was resolved (PR merged, commit,
    # deploy state). Written for the solved-problem archive; supplied by
    # `claustrum done`. Never a raw secret/PII.
    resolution: str | None = None


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
