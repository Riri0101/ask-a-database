import sqlite3
import sys
from pathlib import Path

# Folder holding one sub-folder per Spider database, e.g. database/concert_singer/concert_singer.sqlite
DATABASE_DIR = Path(__file__).resolve().parent.parent / "data" / "spider" / "database"


def get_db_path(db_name: str, db_dir: Path | None = None) -> Path:
    """Return the path to a database's .sqlite file.

    `db_dir` is the folder of database sub-folders (default: the Spider databases);
    BIRD uses the same <db_dir>/<name>/<name>.sqlite layout.
    """
    db_path = (db_dir or DATABASE_DIR) / db_name / f"{db_name}.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"No database found at {db_path}")
    return db_path


def get_schema_text(db_name: str, num_rows: int = 3, db_dir: Path | None = None) -> str:
    """Describe a database file as text: tables, columns, keys, and a few sample rows."""
    conn = sqlite3.connect(get_db_path(db_name, db_dir))
    # Some Spider databases contain text that isn't valid UTF-8; don't crash on it.
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    try:
        return describe_connection(conn, db_name, num_rows)
    finally:
        conn.close()


def _shorten(value, max_chars: int | None):
    """Cut long text values down to max_chars (None: leave everything as is)."""
    if max_chars is not None and isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "..."
    return value


def describe_connection(
    conn: sqlite3.Connection, db_name: str, num_rows: int = 3, max_value_chars: int | None = None
) -> str:
    """Describe an open database (a file or an in-memory one) as text.

    `max_value_chars` shortens long text in the sample rows, so less data is sent to the model.
    """
    cursor = conn.cursor()

    # SQLite keeps a list of its own tables in a built-in table called sqlite_master.
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names = [row[0] for row in cursor.fetchall()]

    lines = [f"Database: {db_name}"]

    for table in table_names:
        lines.append(f"\nTable: {table}")

        # PRAGMA table_info gives one row per column: (position, name, type, notnull, default, pk_position)
        cursor.execute(f'PRAGMA table_info("{table}")')
        columns = cursor.fetchall()
        lines.append("Columns:")
        for col in columns:
            col_name, col_type, is_pk = col[1], col[2] or "UNKNOWN", col[5]
            pk_note = " PRIMARY KEY" if is_pk else ""
            lines.append(f"  - {col_name} {col_type}{pk_note}")

        # PRAGMA foreign_key_list gives (id, seq, other_table, from_column, to_column, ...)
        cursor.execute(f'PRAGMA foreign_key_list("{table}")')
        foreign_keys = cursor.fetchall()
        if foreign_keys:
            lines.append("Foreign keys:")
            for fk in foreign_keys:
                other_table, from_col, to_col = fk[2], fk[3], fk[4]
                lines.append(f"  - {table}.{from_col} -> {other_table}.{to_col}")

        cursor.execute(f'SELECT * FROM "{table}" LIMIT {num_rows}')
        rows = cursor.fetchall()
        lines.append(f"Sample rows ({len(rows)}):")
        if not rows:
            lines.append("  (table is empty)")
        for row in rows:
            if max_value_chars is not None:
                row = tuple(_shorten(value, max_value_chars) for value in row)
            lines.append(f"  {row}")

    return "\n".join(lines)


def show_database(db_name: str, num_rows: int = 3) -> None:
    """Print the schema text for a database."""
    print(get_schema_text(db_name, num_rows))


if __name__ == "__main__":
    # Lets you run: python agent/schema.py concert_singer
    show_database(sys.argv[1] if len(sys.argv) > 1 else "concert_singer")
