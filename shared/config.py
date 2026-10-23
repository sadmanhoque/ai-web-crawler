import os
from functools import lru_cache
import yaml
from pydantic import BaseModel

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STREAM, DEAD_STREAM, GROUP = "index:events", "index:dead", "indexers"


class Profile(BaseModel):
    name: str = ""
    description: str = ""
    collection: str
    embedding_model: str
    vector_size: int
    chunk_size: int
    chunk_overlap: int


@lru_cache
def load_profiles() -> dict[str, Profile]:
    raw = yaml.safe_load(open(os.getenv("PROFILES_FILE", "config/profiles.yaml")))["profiles"]
    profiles = {k: Profile(name=k, **v) for k, v in raw.items()}
    by_coll: dict[str, Profile] = {}
    for p in profiles.values():
        other = by_coll.setdefault(p.collection, p)
        if (other.embedding_model, other.vector_size) != (p.embedding_model, p.vector_size):
            raise ValueError(f"Profiles '{other.name}' and '{p.name}' share collection "
                             f"'{p.collection}' but use different embedding configs")
    return profiles
