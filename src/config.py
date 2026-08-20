import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

    sqlite_db_path: str = os.getenv("SQLITE_DB_PATH", "./data/marketplace.db")

    qdrant_host: str = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port: int = int(os.getenv("QDRANT_PORT", "6333"))
    correction_collection: str = os.getenv(
        "CORRECTION_COLLECTION", "analyst_agent_corrections"
    )
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "all-minilm")

    # Lowered from 3 -> 2 (ADR-0002, Decision 5): fewer forced attempts,
    # bias toward honest escalation over a plausible-looking wrong answer.
    max_sql_retries: int = int(os.getenv("MAX_SQL_RETRIES", "2"))
    retrieval_similarity_threshold: float = float(
        os.getenv("RETRIEVAL_SIMILARITY_THRESHOLD", "0.75")
    )

    # ADR-0002: reliability / oversight settings
    node_timeout_seconds: float = float(
        os.getenv("NODE_TIMEOUT_SECONDS", "30"))
    run_timeout_seconds: float = float(os.getenv("RUN_TIMEOUT_SECONDS", "120"))
    clarification_enabled: bool = (
        os.getenv("CLARIFICATION_ENABLED", "true").lower() == "true"
    )
    semantic_critic_enabled: bool = (
        os.getenv("SEMANTIC_CRITIC_ENABLED", "true").lower() == "true"
    )

    # ADR-0009: Langfuse tracing — disabled by default, degrades silently
    # if enabled but unreachable/misconfigured (never breaks a run).
    langfuse_enabled: bool = os.getenv(
        "LANGFUSE_ENABLED", "false").lower() == "true"
    langfuse_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    langfuse_host: str = os.getenv(
        "LANGFUSE_HOST", "https://cloud.langfuse.com")


settings = Settings()
