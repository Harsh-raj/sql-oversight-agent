"""
Parses the schema_tool's text format (e.g. "Table `name`: col1 (TYPE),
col2 (TYPE)") back into structured names. Shared by sql_generator (which
restates table names explicitly in its prompt) and identifier_validator
(which checks generated SQL against these same names).
"""

import re


def extract_table_names(schema_context: str) -> list:
    return re.findall(r"Table `([^`]+)`", schema_context)


def extract_all_column_names(schema_context: str) -> list:
    """
    Returns a flat list of column names across all tables in the schema,
    without table-qualification. Sufficient for a single-table schema;
    a multi-table schema with overlapping column names would need
    table-qualified matching to avoid ambiguity — a known limitation for
    now (see ADR-0005, Decision 4).
    """
    columns = []
    for line in schema_context.splitlines():
        match = re.match(r"Table `[^`]+`:\s*(.*)", line)
        if not match:
            continue
        for col_def in match.group(1).split(","):
            col_name = col_def.strip().split(" ")[0]
            if col_name:
                columns.append(col_name)
    return columns
