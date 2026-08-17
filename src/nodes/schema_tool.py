import sqlite3

from src.config import settings
from src.state import AgentState


def get_schema_context(state: AgentState) -> AgentState:
    """
    Introspects the sandbox DB and produces a compact schema description
    for the SQL generator prompt. Grounding on the *actual* schema (rather
    than trusting the model to remember it) is the single biggest lever
    for reducing hallucinated columns/tables in a small local model.
    """
    conn = sqlite3.connect(settings.sqlite_db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    lines = []
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = cursor.fetchall()  # cid, name, type, notnull, dflt_value, pk
        col_desc = ", ".join(f"{col[1]} ({col[2]})" for col in columns)
        lines.append(f"Table `{table}`: {col_desc}")

    conn.close()

    return {**state, "schema_context": "\n".join(lines)}
