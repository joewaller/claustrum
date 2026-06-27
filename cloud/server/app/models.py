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
    # The session's domain (mirrored down so the local DB is offline-joinable).
    domain: str | None = None
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
    # Domain to record on the session. Optional: when omitted, classify_self
    # derives it from topics.domain for the chosen topic (NULL if the topic isn't
    # in the taxonomy). Lets a session classified into a brand-new topic still
    # carry a domain for the board before that topic is joinable.
    domain: str | None = None


class ProposeTopicRequest(BaseModel):
    uid: str
    name: str
    description: str
    # Domain this topic belongs to. Optional on the wire (defaults to 'general'
    # server-side) so the existing 3-arg propose path keeps working until callers
    # send a real domain; validated against `domains` when supplied.
    domain: str | None = None


class TopicEntry(BaseModel):
    name: str
    description: str
    # When set, this name is a variant that resolves to the canonical `parent`
    # topic (e.g. gateway -> mcp-gateway) — so consumers can collapse duplicates.
    parent: str | None = None
    source: str
    # The domain this topic belongs to (NOT NULL in the DB — always present).
    domain: str


class TopicsResponse(BaseModel):
    topics: list[TopicEntry]


class RegisterTopicRequest(BaseModel):
    # Canonical kebab name to add to the taxonomy if absent. Lowercased server-side.
    name: str
    description: str
    parent: str | None = None
    # Optional (defaults to 'general' server-side) so the memory-enhanced
    # registrar — which doesn't send a domain yet (Phase 3) — keeps working.
    domain: str | None = None


class RegisterTopicResponse(BaseModel):
    ok: bool = True
    name: str
    # True if this call inserted the topic; False if it already existed.
    created: bool


# ---------------------------------------------------------------------------
# Domains — first-class taxonomy mirroring topics (emergent: bootstrap seeds,
# registrar register, propose -> promote at the distinct-user threshold).
# ---------------------------------------------------------------------------
class ProposeDomainRequest(BaseModel):
    uid: str
    name: str
    description: str


class DomainEntry(BaseModel):
    name: str
    description: str
    # When set, a variant that resolves to the canonical `parent` domain.
    parent: str | None = None
    source: str


class DomainsResponse(BaseModel):
    domains: list[DomainEntry]


class RegisterDomainRequest(BaseModel):
    # Canonical kebab name to add to the domain taxonomy if absent. Lowercased.
    name: str
    description: str
    parent: str | None = None


class RegisterDomainResponse(BaseModel):
    ok: bool = True
    name: str
    # True if this call inserted the domain; False if it already existed.
    created: bool


class OkResponse(BaseModel):
    ok: bool = True
