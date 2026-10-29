"""Turn uploaded CSV files into a temporary in-memory SQLite database.

Nothing here touches the disk: the database lives in memory (":memory:") and is
kept in one visitor's session only. Every problem with a file becomes a friendly
message instead of a crash.
"""

import io
import re
import sqlite3
import unicodedata
from collections import Counter

import pandas as pd

MAX_FILES = 3
MAX_FILE_MB = 5
MAX_COLUMNS = 100
DB_NAME = "your_data"

# Words that would confuse SQL if used as a name; such names get a suffix (e.g. "order_col").
SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "by", "having", "limit", "join", "on",
    "and", "or", "not", "in", "is", "null", "as", "table", "index", "union", "case",
    "when", "then", "else", "end", "values", "insert", "update", "delete", "create",
    "drop", "distinct", "between", "like", "exists", "all", "any", "primary", "key",
}

# Date formats we recognise, tried in order (month-first before day-first).
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M",
    "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y",
]


class UploadError(Exception):
    """A problem with one file, worded for the user."""


def clean_name(raw, fallback: str, suffix: str) -> str:
    """Make a SQL-friendly name: lowercase letters, digits and underscores only.

    "Order Date (UTC)" -> "order_date_utc", "Café" -> "cafe", "2024 sales" -> "c_2024_sales".
    """
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode()
    name = re.sub(r"[^0-9a-z]+", "_", text.strip().lower()).strip("_")
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"{suffix[0]}_{name}"
    if name in SQL_KEYWORDS:
        name = f"{name}_{suffix}"
    return name


def make_unique(names: list[str], taken: set[str] | None = None) -> list[str]:
    """Add _2, _3, ... to repeated names: ["name", "name"] -> ["name", "name_2"]."""
    taken = set(taken or ())
    result = []
    for name in names:
        candidate, n = name, 1
        while candidate in taken:
            n += 1
            candidate = f"{name}_{n}"
        taken.add(candidate)
        result.append(candidate)
    return result


def decode(raw: bytes) -> tuple[str, str]:
    """Turn file bytes into text, trying common encodings. Returns (text, encoding used)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        attempts = ["utf-16"]
    else:
        attempts = ["utf-8-sig", "cp1252", "latin-1"]  # latin-1 accepts any bytes, so it's the last resort
    for encoding in attempts:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UploadError("couldn't be read as text. Please save it as a UTF-8 CSV and try again.")
    if "\x00" in text:
        raise UploadError("doesn't look like a CSV text file. Please export it as CSV and try again.")
    return text, encoding


def sniff_delimiter(text: str) -> str:
    """Guess whether values are separated by commas, semicolons, tabs or pipes.

    Picks the character that appears the same number of times on most of the first
    lines (a broken row or an apostrophe in a name doesn't throw it off).
    """
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    best, best_score = ",", (0.0, 0)
    for delimiter in ",;\t|":
        counts = [line.count(delimiter) for line in lines]
        typical = Counter(counts).most_common(1)[0][0] if counts else 0
        if typical == 0:
            continue
        score = (counts.count(typical) / len(counts), typical)  # how consistent, then how many columns
        if score > best_score:
            best, best_score = delimiter, score
    return best


def detect_type(values: pd.Series) -> str:
    """Pick INTEGER, REAL, DATE or TEXT for a column of strings (empty values ignored)."""
    values = values.dropna().str.strip()
    values = values[values != ""]
    if values.empty:
        return "TEXT"
    # Codes like ZIP "02139" must stay text, or the leading zero is lost.
    has_leading_zero = values.str.match(r"^-?0\d").any()
    numbers = pd.to_numeric(values, errors="coerce")
    if not has_leading_zero and numbers.notna().all():
        return "INTEGER" if (numbers % 1 == 0).all() and not values.str.contains(r"[.eE]").any() else "REAL"
    if date_format(values):
        return "DATE"
    return "TEXT"


def date_format(values: pd.Series) -> str | None:
    """The first DATE_FORMATS entry that every value matches, or None."""
    for fmt in DATE_FORMATS:
        if pd.to_datetime(values, format=fmt, errors="coerce").notna().all():
            return fmt
    return None


def convert(values: pd.Series, sql_type: str) -> list:
    """Convert a column of strings to Python values for SQLite (None for empty cells)."""
    stripped = values.where(values.isna(), values.str.strip()).replace("", None)
    if sql_type == "INTEGER":
        return [None if pd.isna(v) else int(v) for v in pd.to_numeric(stripped)]
    if sql_type == "REAL":
        return [None if pd.isna(v) else float(v) for v in pd.to_numeric(stripped)]
    if sql_type == "DATE":
        dates = pd.to_datetime(stripped, format=date_format(stripped.dropna()), errors="coerce")
        with_time = (dates.dropna().dt.normalize() != dates.dropna()).any()
        fmt = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"  # ISO text sorts and compares correctly
        return [None if pd.isna(d) else d.strftime(fmt) for d in dates]
    return [None if pd.isna(v) else v for v in stripped]


def read_csv(raw: bytes, filename: str) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Parse one CSV. Returns (data as strings, header names, notes for the user)."""
    if not raw.strip():
        raise UploadError("is empty.")
    text, encoding = decode(raw)
    notes = []
    if encoding not in ("utf-8-sig", "utf-16"):
        notes.append(f"wasn't UTF-8, so it was read as {encoding}. Check that accented letters look right.")

    skipped = []
    try:
        df = pd.read_csv(
            io.StringIO(text), sep=sniff_delimiter(text), header=None, dtype=str,
            engine="python", on_bad_lines=lambda line: skipped.append(line) and None,
        )
    except pd.errors.EmptyDataError:
        raise UploadError("is empty.")
    except (pd.errors.ParserError, ValueError) as e:
        raise UploadError(f"couldn't be read as a CSV ({e}).")
    if skipped:
        notes.append(f"had {len(skipped)} row(s) with the wrong number of values, which were skipped.")

    # The first row holds the column names (read separately so duplicates aren't renamed by pandas).
    header, df = list(df.iloc[0]), df.iloc[1:].reset_index(drop=True)
    df = df.dropna(how="all")  # blank lines
    keep = [i for i, h in enumerate(header) if not (pd.isna(h) and df[df.columns[i]].isna().all())]
    if len(keep) < len(header):
        notes.append(f"had {len(header) - len(keep)} empty, unnamed column(s), which were left out.")
    df, header = df[[df.columns[i] for i in keep]], [header[i] for i in keep]
    if not header:
        raise UploadError("has no columns.")
    if len(header) > MAX_COLUMNS:
        raise UploadError(f"has {len(header)} columns; the limit is {MAX_COLUMNS}. Please remove some and try again.")
    if df.empty:
        raise UploadError("has column names but no data rows.")
    return df, header, notes


def load_csv_files(files: list[tuple[str, bytes]]) -> dict:
    """Load up to MAX_FILES CSVs into one new in-memory database.

    `files` is [(filename, bytes), ...]. Returns
    {"conn": connection or None, "tables": [...], "errors": [...], "notes": [...]}.
    Each table is {"name", "file", "rows", "columns": [{"name", "type", "original"}]}.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)  # used across Streamlit reruns
    tables, errors, notes = [], [], []
    for filename, raw in files[:MAX_FILES]:
        try:
            if len(raw) > MAX_FILE_MB * 1024 * 1024:
                raise UploadError(f"is larger than {MAX_FILE_MB} MB.")
            df, header, file_notes = read_csv(raw, filename)
        except UploadError as e:
            errors.append(f"{filename} {e}")
            continue
        notes += [f"{filename} {note}" for note in file_notes]

        stem = filename.rsplit(".", 1)[0]
        table = make_unique([clean_name(stem, "table", "table")], {t["name"] for t in tables})[0]
        names = make_unique([clean_name(h, f"column_{i + 1}", "col") for i, h in enumerate(header)])
        types = [detect_type(df[c]) for c in df.columns]
        columns_sql = ", ".join(f'"{n}" {t}' for n, t in zip(names, types))
        conn.execute(f'CREATE TABLE "{table}" ({columns_sql})')
        values = list(zip(*(convert(df[c], t) for c, t in zip(df.columns, types))))
        conn.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(names))})', values)
        tables.append({
            "name": table, "file": filename, "rows": len(values),
            "columns": [
                {"name": n, "type": t, "original": "" if pd.isna(h) else str(h)}
                for n, t, h in zip(names, types, header)
            ],
        })

    conn.commit()
    conn.execute("PRAGMA query_only = ON")  # from now on, nothing can change the data
    if not tables:
        conn.close()
        conn = None
    return {"conn": conn, "tables": tables, "errors": errors, "notes": notes}
