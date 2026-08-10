"""
Database Session
================

PostgreSQL connection helpers. Use:
- get_postgres_db() for agent storage backed by Postgres.
- create_knowledge() for agent knowledge backed by PgVector.
"""

import hashlib
from functools import cache
from os import getenv

from agno.db.postgres import PostgresDb
from agno.knowledge import Knowledge
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.vectordb.pgvector import PgVector, SearchType

from db.url import db_url

DB_ID = "agentos-db"


class KeywordStorageEmbedder(Embedder):
    """Create schema-compatible vectors that are ignored by keyword search."""

    def __init__(self, dimensions: int = 1536):
        super().__init__(dimensions=dimensions)

    def get_embedding(self, text: str) -> list[float]:
        dimensions = self.dimensions or 1536
        vector = [0.0] * dimensions
        position = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") % dimensions
        vector[position] = 1.0
        return vector

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict | None]:
        return self.get_embedding(text), None

    async def async_get_embedding(self, text: str) -> list[float]:
        return self.get_embedding(text)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict | None]:
        return self.get_embedding_and_usage(text)


@cache
def get_postgres_db(contents_table: str | None = None) -> PostgresDb:
    """Returns the shared PostgresDb instance for the AgentOS.

    Memoized so every agent/workflow/schedule reuses the same object
    instead of constructing a fresh PostgresDb on each call.

    Pass contents_table when this database is used as the contents_db of a Knowledge base.
    For plain agent persistence (sessions, memory), leave it unset.
    """
    if contents_table is not None:
        return PostgresDb(id=DB_ID, db_url=db_url, knowledge_table=contents_table)
    return PostgresDb(id=DB_ID, db_url=db_url)


def create_knowledge(name: str, table_name: str) -> Knowledge:
    """Create keyword knowledge unless a dedicated embedding model is configured."""
    embedding_model = getenv("OPENAI_EMBEDDING_MODEL_ID")
    if embedding_model:
        embedder: Embedder = OpenAIEmbedder(
            id=embedding_model,
            api_key=getenv("OPENAI_EMBEDDING_API_KEY") or None,
            base_url=getenv("OPENAI_EMBEDDING_BASE_URL") or None,
        )
        search_type = SearchType.hybrid
    else:
        embedder = KeywordStorageEmbedder()
        search_type = SearchType.keyword
    return Knowledge(
        name=name,
        vector_db=PgVector(
            db_url=db_url,
            table_name=table_name,
            search_type=search_type,
            embedder=embedder,
        ),
        contents_db=get_postgres_db(contents_table=f"{table_name}_contents"),
    )
