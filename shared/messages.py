"""Contract between crawler and indexer (Redis Stream)."""
from enum import Enum
from typing import Annotated, Literal, Union
import redis
from pydantic import BaseModel, Field, TypeAdapter
from .config import REDIS_URL, STREAM
from .tiers import Tier


class Depth(str, Enum):
    headline = "headline"   # title + h1 + meta description only
    summary = "summary"     # LLM summary of the page
    full = "full"           # every section


class Section(BaseModel):
    heading: str
    heading_tag: str
    text: str


class PageMsg(BaseModel):
    type: Literal["page"] = "page"
    crawl_id: str
    site_id: int
    domain: str
    profile: str
    collection: str
    url: str
    title: str
    depth: Depth
    sections: list[Section]


class CloseMsg(BaseModel):
    """Sent once per crawl. complete=True lets the indexer delete stale points."""
    type: Literal["close"] = "close"
    crawl_id: str
    site_id: int
    collection: str
    total: int
    complete: bool


class MoveMsg(BaseModel):
    type: Literal["move"] = "move"
    site_id: int
    profile: str
    src: str
    dst: str


class DemoteMsg(BaseModel):
    """Tier -> t3: remove the page's vectors from Qdrant. The tier bot has
    already written the text to page_archives before publishing this."""
    type: Literal["demote"] = "demote"
    site_id: int
    collection: str
    url: str
    to_tier: Tier = Tier.t3


class PromoteMsg(BaseModel):
    """Tier up: delete the page's old points and re-index the provided
    sections (fetched by the tier bot) at the target tier."""
    type: Literal["promote"] = "promote"
    site_id: int
    domain: str
    profile: str
    collection: str
    url: str
    title: str
    to_tier: Tier
    sections: list[Section]


Event = Annotated[Union[PageMsg, CloseMsg, MoveMsg, DemoteMsg, PromoteMsg],
                  Field(discriminator="type")]
_adapter = TypeAdapter(Event)


def parse_event(data: str):
    return _adapter.validate_json(data)


def get_redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def publish(r: redis.Redis, msg: BaseModel):
    r.xadd(STREAM, {"data": msg.model_dump_json()}, maxlen=1_000_000, approximate=True)
