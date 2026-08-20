import sqlite3

from src.config import settings
from src.state import AgentState

# Deliberately read-only: any query containing these keywords is rejected
# before it ever touches the DB. This is a cheap but real guardrail —
# in a production version this would be a DB-level read-only role instead
# of a string check, but for the sandbox this keeps the demo honest about
# what "autonomous" is allowed to touch.
_FORBIDDEN_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE")


def execute_sql(state: AgentState) -> AgentState:
    sql = state.get("sql_query", "")

    if any(kw in sql.upper() for kw in _FORBIDDEN_KEYWORDS):
        return {
            **state,
            "sql_result": None,
            "sql_error": (
                "Query rejected: contains a write/DDL keyword. "
                "This agent is read-only by design."
            ),
        }

    try:
        conn = sqlite3.connect(settings.sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [desc[0]
                   for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        conn.close()

        result = [dict(zip(columns, row)) for row in rows]
        return {**state, "sql_result": result, "sql_error": None}

    except sqlite3.Error as e:
        return {**state, "sql_result": None, "sql_error": str(e)}
