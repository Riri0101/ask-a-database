"""Read-only safety for the app: only single SELECT queries may run.

Three layers, so one mistake can't slip through:
1. is_read_only_sql() checks the text: one statement, starting with SELECT or WITH,
   with no data-changing keywords.
2. run_read_only() opens the database file in read-only mode. (Uploaded data lives in
   an in-memory database set to read-only with PRAGMA query_only after loading.)
3. While the query runs, a SQLite "authorizer" refuses every action except reading.
"""

import re
import sqlite3
import time
from pathlib import Path

FORBIDDEN = (
    "INSERT", "UPDATE", "DELETE", "REPLACE", "UPSERT", "DROP", "CREATE", "ALTER",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "TRUNCATE", "GRANT",
)

# Actions a plain SELECT needs; the authorizer denies everything else.
ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE
}


def _strip_strings_and_comments(sql: str) -> str:
    """Blank out quoted text and comments, so words inside them aren't mistaken for commands."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", sql)


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    """Return (True, "") if the query is a single read-only SELECT, else (False, reason)."""
    code = _strip_strings_and_comments(sql).strip().rstrip(";").strip()
    if not code:
        return False, "There was no query to run."
    if ";" in code:
        return False, "Only a single query is allowed."
    if not re.match(r"(SELECT|WITH)\b", code, re.IGNORECASE):
        return False, "Only SELECT queries are allowed."
    for word in FORBIDDEN:
        if re.search(rf"\b{word}\b", code, re.IGNORECASE):
            return False, f"This query contains {word}, which could change data, so it was not run."
    return True, ""


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def run_read_only(sql: str, db_path: Path, timeout_seconds: float = 10) -> dict:
    """Run a checked query on a database file, opened read-only.

    Returns {"ok", "columns", "rows"} or {"ok": False, "error"}.
    """
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    try:
        return run_read_only_connection(sql, conn, timeout_seconds)
    finally:
        conn.close()


def run_read_only_connection(sql: str, conn: sqlite3.Connection, timeout_seconds: float = 10) -> dict:
    """Run a checked query on an open connection (e.g. an in-memory database of uploads).

    The authorizer and time limit are switched on only while this query runs.
    """
    allowed, reason = is_read_only_sql(sql)
    if not allowed:
        return {"ok": False, "error": reason}
    deadline = time.monotonic() + timeout_seconds
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description] if cursor.description else []
        return {"ok": True, "columns": columns, "rows": rows}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
