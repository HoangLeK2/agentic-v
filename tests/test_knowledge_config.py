from os import environ
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agno.vectordb.pgvector import SearchType

from db.session import KeywordStorageEmbedder, create_knowledge


class KnowledgeConfigTest(TestCase):
    def test_missing_embedding_config_uses_keyword_search_without_openai(self) -> None:
        environment = {
            "OPENAI_EMBEDDING_MODEL_ID": "",
            "OPENAI_EMBEDDING_API_KEY": "",
            "OPENAI_EMBEDDING_BASE_URL": "",
        }
        with (
            patch.dict(environ, environment),
            patch("db.session.OpenAIEmbedder") as openai_embedder,
            patch("db.session.Knowledge") as knowledge_factory,
        ):
            create_knowledge("Test", "test_keyword_knowledge")

        vector_db = knowledge_factory.call_args.kwargs["vector_db"]
        self.assertEqual(vector_db.search_type, SearchType.keyword)
        self.assertIsInstance(vector_db.embedder, KeywordStorageEmbedder)
        openai_embedder.assert_not_called()
        vector, usage = vector_db.embedder.get_embedding_and_usage("repeatable")
        self.assertEqual(len(vector), 1536)
        self.assertEqual(sum(vector), 1.0)
        self.assertIsNone(usage)

    def test_explicit_embedding_config_enables_hybrid_search(self) -> None:
        environment = {
            "OPENAI_EMBEDDING_MODEL_ID": "text-embedding-3-small",
            "OPENAI_EMBEDDING_API_KEY": "embedding-key",
            "OPENAI_EMBEDDING_BASE_URL": "https://embedding.example/v1",
        }
        embedder = MagicMock(dimensions=1536)
        with (
            patch.dict(environ, environment),
            patch("db.session.OpenAIEmbedder", return_value=embedder) as factory,
            patch("db.session.Knowledge") as knowledge_factory,
        ):
            create_knowledge("Test", "test_hybrid_knowledge")

        vector_db = knowledge_factory.call_args.kwargs["vector_db"]
        self.assertEqual(vector_db.search_type, SearchType.hybrid)
        self.assertIs(vector_db.embedder, embedder)
        factory.assert_called_once_with(
            id="text-embedding-3-small",
            api_key="embedding-key",
            base_url="https://embedding.example/v1",
        )
