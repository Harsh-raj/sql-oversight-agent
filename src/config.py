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

    # Lowered from 3 -> 2 (ADR-0002, Decision 5): fewer forced attempts,
    # bias toward honest escalation over a plausible-looking wrong answer.
    max_sql_retries: int = int(os.getenv("MAX_SQL_RETRIES", "2"))
    retrieval_similarity_threshold: float = float(
        os.getenv("RETRIEVAL_SIMILARITY_THRESHOLD", "0.75")
    )

    # ADR-0002: reliability / oversight settings
    node_timeout_seconds: float = float(os.getenv("NODE_TIMEOUT_SECONDS", "30"))
    run_timeout_seconds: float = float(os.getenv("RUN_TIMEOUT_SECONDS", "120"))
    clarification_enabled: bool = (
        os.getenv("CLARIFICATION_ENABLED", "true").lower() == "true"
    )


settings = Settings()
