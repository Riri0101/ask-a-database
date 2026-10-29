"""Ask a database in plain English: a Streamlit demo of the text-to-SQL agent.

Run from the project root:
    streamlit run app/streamlit_app.py
"""

import datetime
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # so `import agent` works
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `import safety` and `import uploads` work


def load_api_key() -> None:
    """Use the key from Streamlit secrets when deployed; locally, .env is used instead."""
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:  # no secrets file when running locally
        key = None
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


load_api_key()
from agent.answers import (  # noqa: E402
    check_numbers, collect_facts, template_answer, unsupported_ranking, use_readable_names, write_answer,
)
from agent.schema import describe_connection, get_schema_text  # noqa: E402
from agent.sql_agent import generate_sql_with_usage  # noqa: E402  (also loads .env)
from safety import is_read_only_sql, run_read_only, run_read_only_connection  # noqa: E402
from uploads import DB_NAME, MAX_FILE_MB, MAX_FILES, load_csv_files  # noqa: E402

# ---- Settings ---------------------------------------------------------------
MODEL = "claude-haiku-5-5"  # the model the evaluation measured
EFFORT = "medium"
AGENT_VERSION = "v4sc"  # v4 + self-correction (retries on errors or empty results, up to 2 times)
ANSWER_EFFORT = "low"  # the plain-English answer is a short writing task
DAILY_LIMIT = 200  # questions per day across all visitors (days are in UTC)
SAMPLE_VALUE_CHARS = 100  # uploaded data: long text in the 3 sample rows is cut to this length
# -----------------------------------------------------------------------------

DB_DIR = PROJECT_ROOT / "app_data" / "databases"
USAGE_FILE = PROJECT_ROOT / "app_state" / "usage.json"  # not committed; see .gitignore
UPLOAD = "__upload__"  # the "Use my own data" choice in the database picker

# Example questions were all answered correctly by the agent in the Spider dev evaluation.
DATABASES = {
    "concert_singer": {
        "label": "Concerts and singers",
        "about": "Singers, the stadiums they performed in, and which concerts they sang at.",
        "examples": [
            "How many singers are from each country?",
            "What are the names of the stadiums without any concerts?",
            "What are the names and release years for all the songs of the youngest singer?",
        ],
    },
    "pets_1": {
        "label": "Students and pets",
        "about": "College students and the pets they own.",
        "examples": [
            "What is the average weight for each type of pet?",
            "How many dog pets are raised by female students?",
            "What is the average age for all students who do not own any pets?",
        ],
    },
    "world_1": {
        "label": "World countries",
        "about": "Countries, cities and the languages spoken in them.",
        "examples": [
            "Which continent is Anguilla in?",
            "Return the names and surface areas of the 5 largest countries.",
            "Which language is spoken by the largest number of countries?",
        ],
    },
    "car_1": {
        "label": "Cars (1970s and 80s)",
        "about": "Car makers, models and specs such as horsepower and weight.",
        "examples": [
            "How many car makers are there in france?",
            "What is the average horsepower for all cars produced before 1980?",
            "What are the names of the countries with no car makers?",
        ],
    },
    "orchestra": {
        "label": "Orchestras",
        "about": "Orchestras, their conductors and their performances.",
        "examples": [
            "Show the names of conductors and the orchestras they have conducted.",
            "What are the orchestras that do not have any performances?",
            "List names of conductors in descending order of years of work.",
        ],
    },
}


def db_path(db: str) -> Path:
    return DB_DIR / db / f"{db}.sqlite"


@st.cache_data
def describe_tables(db: str) -> dict[str, list[tuple[str, str]]]:
    """{table: [(column, TYPE), ...]} for the schema browser."""
    conn = sqlite3.connect(db_path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        return {
            t: [(c[1], c[2] or "any") for c in conn.execute(f'PRAGMA table_info("{t}")')]
            for t in tables
        }
    finally:
        conn.close()


# ---- Daily limit, shared by every visitor -------------------------------------

@st.cache_resource
def shared_state() -> dict:
    """One object shared by all visitors: a lock, and today's answers (so repeats are free)."""
    return {"lock": threading.Lock(), "day": None, "answers": {}}


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def read_usage() -> int:
    """How many questions have been asked today (stored in a small file)."""
    try:
        usage = json.loads(USAGE_FILE.read_text())
        return usage["count"] if usage.get("day") == today() else 0
    except (FileNotFoundError, ValueError, KeyError):
        return 0


def write_usage(count: int) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps({"day": today(), "count": count}))


# ---- Answering a question ----------------------------------------------------

def compute_answer(
    question: str, db_name: str, run, schema_text: str, table_columns: dict, names: dict[str, str], **source
) -> dict:
    """Ask the agent for SQL, run it with the read-only guard, then write the answer.

    `run(sql)` runs a query safely; it is also what the agent uses when self-correcting.
    `schema_text` and `table_columns` ({table: [(column, type)]}) describe the data for
    the answer writer. `source` says where the agent gets the schema: db_dir=... for a
    built-in database, or schema=... (text) for uploaded data.
    """
    answer = generate_sql_with_usage(
        question, db_name, model=MODEL, effort=EFFORT, version=AGENT_VERSION, run=run, **source
    )
    sql = answer["sql"]
    result = run(sql)
    reply = write_reply(question, sql, result, schema_text, table_columns, run, names) if result["ok"] else None
    return {
        "sql": sql, "result": result, "reply": reply,
        "retried": answer["attempts"] > 1, "attempts": answer["attempts"],
        "names": names,  # readable table and column names, for the result table's headers
    }


def write_reply(
    question: str, sql: str, result: dict, schema_text: str, table_columns: dict, run, names: dict[str, str]
) -> dict:
    """The plain-English answer: the model's sentence if it passes both checks, else a template.

    Two checks on the model's sentence:
    - numbers: every number must match the result (or the question, SQL, data facts);
    - ranking: words like "latest" or "highest" need an ORDER BY / MAX / MIN / LIMIT in the SQL.
    `names` maps raw table and column names to readable ones ("gross_profit" -> "Gross Profit").
    Returns {"text", "assumptions", "alternatives", "check", "unmatched", "ranking_words"}, where
    check is "passed", "failed" (numbers), "ranking" (unbacked ranking words) or "unavailable"
    (the writer call failed); in every case but "passed" the template answer is shown.
    """
    columns, rows = result["columns"], result["rows"]
    template = template_answer(columns, rows, lambda c: column_label(c, names))
    facts = collect_facts(run, table_columns, tables_used(sql, list(table_columns)))
    reply = {"text": template, "assumptions": [], "alternatives": [], "check": "unavailable",
             "unmatched": [], "ranking_words": []}
    try:
        drafted = write_answer(question, sql, columns, rows, schema_text, facts, MODEL, effort=ANSWER_EFFORT)
    except Exception:  # the answer is still shown, from the template
        return reply

    allowed = [question, sql, facts]  # numbers from these may appear in the answer too
    numbers_ok, unmatched = check_numbers(drafted["answer"], rows, allowed)
    ranking_words = unsupported_ranking(drafted["answer"], sql)
    if ranking_words:
        reply["check"] = "ranking"
    elif not numbers_ok:
        reply["check"] = "failed"
    else:
        reply["check"], reply["text"] = "passed", drafted["answer"]
    reply.update(
        unmatched=unmatched,
        ranking_words=ranking_words,
        assumptions=[
            a for a in drafted["assumptions"]
            if check_numbers(a, rows, allowed)[0] and not unsupported_ranking(a, sql)
        ],
        alternatives=drafted["alternatives"],
    )
    # Readable column names everywhere the user reads them.
    reply["text"] = use_readable_names(reply["text"], names)
    reply["assumptions"] = [use_readable_names(a, names) for a in reply["assumptions"]]
    reply["alternatives"] = [use_readable_names(a, names) for a in reply["alternatives"]]
    return reply


def readable_names(table_columns: dict, upload: dict | None) -> dict[str, str]:
    """Raw table and column names -> readable ones, for everything the user reads.

    Uploads use the original file name and CSV headers ("spotify_quarterly" -> "Spotify Quarterly",
    "gross_profit" -> "Gross Profit"); built-in names become words ("song_release_year" ->
    "Song Release Year"). Only names with underscores are included, so ordinary words in a
    sentence ("each region") are never changed.
    """
    def words(name: str) -> str:
        return " ".join(w[:1].upper() + w[1:] for w in name.split("_") if w)

    if upload:
        names = {t["name"]: t["file"].rsplit(".", 1)[0] for t in upload["tables"]}
        names |= {c["name"]: c["original"] or words(c["name"]) for t in upload["tables"] for c in t["columns"]}
    else:
        names = {table: words(table) for table in table_columns}
        names |= {name: words(name) for columns in table_columns.values() for name, _ in columns}
    return {raw: nice for raw, nice in names.items() if "_" in raw}


def ask(scope: str, question: str, compute, share: bool = True) -> dict:
    """Answer a question, respecting the daily limit. Returns {"limit_reached": True} when it's hit.

    `compute()` produces the answer. With `share`, today's answer is kept and reused for
    anyone asking the same question; uploaded data is never shared.
    """
    state = shared_state()
    key = (scope, " ".join(question.lower().split()))
    with state["lock"]:
        if state["day"] != today():  # a new day: start fresh
            state["day"], state["answers"] = today(), {}
        if share and key in state["answers"]:  # asked before today: reuse it, doesn't count
            return state["answers"][key]
        used = read_usage()
        if used >= DAILY_LIMIT:
            return {"limit_reached": True}
        write_usage(used + 1)  # reserve this question's slot before calling the model

    try:
        out = compute()
    except Exception as e:  # API trouble: give the slot back and report it
        with state["lock"]:
            write_usage(max(read_usage() - 1, 0))
        return {"api_error": str(e)}
    if share:
        with state["lock"]:
            state["answers"][key] = out
    return out


def to_dataframe(result: dict) -> pd.DataFrame:
    """Turn query rows into a table, renaming repeated column names (e.g. two 'Name's)."""
    seen, columns = {}, []
    for col in result["columns"]:
        seen[col] = seen.get(col, 0) + 1
        columns.append(col if seen[col] == 1 else f"{col} ({seen[col]})")
    return pd.DataFrame(result["rows"], columns=columns)


# ---- Uploaded data ------------------------------------------------------------

def load_uploads() -> dict | None:
    """The sidebar uploader; loads the files into this visitor's in-memory database.

    Returns the loaded upload ({"conn", "tables", "schema", "errors", "notes"}),
    {"too_many": n} when more than MAX_FILES are chosen, or None when nothing is uploaded.
    """
    st.caption(
        "Don't upload sensitive or personal data. Files are only kept for your session. "
        "To answer, the AI model is sent your table and column names, 3 sample rows per table, "
        "and the query results."
    )
    files = st.file_uploader(
        f"Upload up to {MAX_FILES} CSV files ({MAX_FILE_MB} MB each)", type="csv", accept_multiple_files=True
    )
    upload = st.session_state.get("upload")
    if not files:
        if upload and upload["conn"]:
            upload["conn"].close()
        st.session_state.pop("upload", None)
        return None
    if len(files) > MAX_FILES:
        return {"too_many": len(files)}

    # Load only when the set of files changes, not on every click.
    signature = tuple((f.file_id, f.name, f.size) for f in files)
    if not upload or upload["signature"] != signature:
        if upload and upload["conn"]:
            upload["conn"].close()
        with st.spinner("Reading your files..."):
            upload = load_csv_files([(f.name, f.getvalue()) for f in files])
        upload["signature"] = signature
        # Only this text (table and column names, types, 3 sample rows per table) is sent to the model.
        upload["schema"] = (
            describe_connection(upload["conn"], DB_NAME, max_value_chars=SAMPLE_VALUE_CHARS) if upload["conn"] else ""
        )
        st.session_state["upload"] = upload
    return upload


# ---- Presenting answers ------------------------------------------------------

AGGREGATE_WORDS = {"count": "Number of", "sum": "Total", "avg": "Average", "max": "Highest", "min": "Lowest"}


def humanize(name: str) -> str:
    """'T1.pet_age' -> 'Pet age'."""
    name = name.split(".")[-1].strip('"`[] ').replace("_", " ").strip()
    return name[0].upper() + name[1:] if name else "Value"


def column_label(column: str, names: dict[str, str] | None = None) -> str:
    """A readable label for a result column: 'AVG(weight)' -> 'Average weight', 'count(*)' -> 'Count'.

    With `names` (raw -> readable, see readable_names), known names are used as they are:
    'gross_profit' -> 'Gross Profit', 'SUM(gross_profit)' -> 'Total Gross Profit'.
    """
    names = names or {}
    match = re.fullmatch(r"\s*(count|sum|avg|max|min)\s*\(\s*(?:distinct\s+)?(.*?)\s*\)\s*", column, re.IGNORECASE)
    if not match:
        bare = column.split(".")[-1].strip('"`[] ')
        return names.get(bare) or humanize(column)
    func, inner = match.group(1).lower(), match.group(2)
    if inner in ("", "*"):
        return "Count"
    bare = inner.split(".")[-1].strip('"`[] ')
    return f"{AGGREGATE_WORDS[func]} {names.get(bare) or humanize(inner).lower()}"


def format_value(value) -> str:
    """Numbers with thousands separators and sensible decimals, for the headline figure."""
    if value is None:
        return "No value"
    if hasattr(value, "item"):  # pandas/numpy numbers (e.g. numpy.int64) -> plain Python numbers
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.4g}"


def chart_spec(df: pd.DataFrame) -> tuple[str, str, str] | None:
    """("bar" or "line", category column, number column) if the result suits one chart, else None.

    Only for exactly one category column plus one number column, 2 to 20 rows.
    A category made of dates gets a line chart; anything else a bar chart.
    """
    if df.shape[1] != 2 or not 2 <= len(df) <= 20:
        return None
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any()]
    if len(numeric) != 1:
        return None
    number = numeric[0]
    category = next(c for c in df.columns if c != number)
    values = df[category].astype(str)
    is_date = values.str.match(r"^\d{4}-\d{2}(-\d{2})?").all() and pd.to_datetime(
        values, format="ISO8601", errors="coerce"
    ).notna().all()
    return ("line" if is_date else "bar"), category, number


def draw_chart(df: pd.DataFrame, kind: str, category: str, number: str) -> None:
    """One simple chart (columns already have readable names)."""
    data = pd.DataFrame({"category": df[category], "value": df[number]})
    whole_numbers = (data["value"].dropna() % 1 == 0).all()
    # Counts get whole-number ticks (no "0.2 singers"); other values get thousands separators.
    value_axis = alt.Axis(tickCount=5, format=",d" if whole_numbers else ",~f", tickMinStep=1 if whole_numbers else 0)
    tooltip = [alt.Tooltip("category", title=category), alt.Tooltip("value:Q", title=number, format=",")]
    if kind == "line":
        data["category"] = pd.to_datetime(data["category"].astype(str), format="ISO8601")
        tooltip[0] = alt.Tooltip("category:T", title=category)
        chart = alt.Chart(data).mark_line(point=True, strokeWidth=2.5).encode(
            x=alt.X("category:T", title=category),
            y=alt.Y("value:Q", title=number, axis=value_axis),
            tooltip=tooltip,
        ).properties(height=260)
    else:
        data["category"] = data["category"].astype(str)
        chart = alt.Chart(data).mark_bar(cornerRadiusEnd=3).encode(
            y=alt.Y("category:N", sort="-x", title=None, axis=alt.Axis(labelLimit=200)),
            x=alt.X("value:Q", title=number, axis=value_axis),
            tooltip=tooltip,
        ).properties(height=alt.Step(28))  # 28px per bar, so bars never squash together
    st.altair_chart(chart, width="stretch")


def readable(df: pd.DataFrame, names: dict[str, str] | None = None) -> pd.DataFrame:
    """Rename result columns for people: 'COUNT(*)' -> 'Count', 'gross_profit' -> 'Gross Profit'."""
    labels, seen = [], {}
    for column in df.columns:
        label = column_label(column, names)
        seen[label] = seen.get(label, 0) + 1
        labels.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return df.set_axis(labels, axis=1)


def tables_used(sql: str, table_names: list[str]) -> list[str]:
    """Tables the query reads from (named after FROM or JOIN)."""
    return [
        t for t in table_names
        if re.search(rf"\b(?:from|join)\s+[\"`\[]?{re.escape(t)}\b", sql or "", re.IGNORECASE)
    ]


def plain(text: str) -> str:
    """Make model-written text safe for st.markdown (a "$" would otherwise start math)."""
    return text.replace("$", "\\$")


def render_answer(entry: dict) -> None:
    """The answer card, then the "How I got this answer" card."""
    out, result = entry["out"], entry["out"]["result"]
    reply = out.get("reply")
    refused = not result["ok"] and not is_read_only_sql(out["sql"] or "")[0]
    retries = out.get("attempts", 2 if out["retried"] else 1) - 1

    with st.container(border=True, key=f"card-answer-{entry['id']}"):
        st.caption(f"{entry['source_label']} · asked at {entry['time']}")
        st.markdown(f"### {plain(entry['question'])}")
        if reply:  # the plain-English answer, large, at the top
            with st.container(key=f"answer-text-{entry['id']}"):
                st.markdown(plain(reply["text"]))
        if result["ok"] and result["rows"]:
            df = readable(to_dataframe(result), out.get("names"))
            if df.shape == (1, 1):
                st.metric(df.columns[0], format_value(df.iat[0, 0]))
            else:
                spec = chart_spec(df)
                if spec:
                    draw_chart(df, *spec)
                st.dataframe(df, hide_index=True, width="stretch")
                st.download_button(
                    "Download CSV", df.to_csv(index=False).encode("utf-8"), file_name="answer.csv",
                    mime="text/csv", icon=":material/download:", on_click="ignore", key=f"download-{entry['id']}",
                )
        elif refused:
            st.error(f"**Not run.** {result['error']} This workspace is read-only.", icon=":material/block:")
        elif not result["ok"]:
            st.warning(f"The query couldn't be run: {result['error']}")

        if reply and reply["assumptions"]:
            st.markdown("**Assumptions:** " + plain("; ".join(reply["assumptions"])))
        if reply and reply["alternatives"]:
            st.caption("Did you mean")
            with st.container(horizontal=True, gap="small", key=f"alternatives-{entry['id']}"):
                for i, alternative in enumerate(reply["alternatives"]):
                    st.button(alternative, key=f"alt-{entry['id']}-{i}", on_click=use_example, args=(alternative,),
                              icon=":material/alt_route:")
        if result["ok"] and (out["retried"] or not result["rows"]):
            st.caption(":material/info: This answer needed a retry or returned nothing, so double-check it.")
        if entry["is_upload"]:
            st.caption("Accuracy was measured on benchmark databases, not on your data. "
                       "Check the SQL before relying on the answer.")

    with st.container(border=True, key=f"card-work-{entry['id']}"):
        st.markdown("#### How I got this answer")
        with st.container(horizontal=True, gap="small"):
            if result["ok"]:
                st.badge("Ran successfully", icon=":material/check_circle:", color="green")
            elif refused:
                st.badge("Refused: not read-only", icon=":material/block:", color="red")
            else:
                st.badge("Couldn't run", icon=":material/error:", color="red")
            if retries:
                st.badge(f"Needed {retries} {'retry' if retries == 1 else 'retries'}", icon=":material/replay:", color="orange")
            if result["ok"] and not result["rows"]:
                st.badge("Returned no rows", icon=":material/do_not_disturb_on:", color="gray")
            if entry["is_upload"]:
                st.badge("Uploaded data: accuracy not measured", icon=":material/upload_file:", color="violet")
            if reply and reply["check"] == "passed":
                st.badge("Answer numbers verified", icon=":material/fact_check:", color="green",
                         help="Every number in the written answer matches the query result, the question or the data.")
            elif reply and reply["check"] in ("failed", "ranking"):
                st.badge("Answer rebuilt from the data", icon=":material/rule:", color="orange")
            elif reply:
                st.badge("Answer from a template", icon=":material/notes:", color="gray")
        if reply and reply["check"] == "failed":
            st.caption(
                f"The written answer mentioned {', '.join(reply['unmatched'])}, which didn't match the query "
                "result, so a simpler answer built directly from the result is shown instead."
            )
        elif reply and reply["check"] == "ranking":
            words = ", ".join(f"“{w}”" for w in reply.get("ranking_words", []))
            st.caption(
                f"The written answer said {words}, but the query didn't sort or pick values (no ORDER BY, "
                "MAX, MIN or LIMIT), so an honest summary of the result is shown instead."
            )
        tables = entry["tables"]
        st.markdown("**Tables used:** " + (", ".join(f"`{t}`" for t in tables) if tables else "none identified"))
        st.code(out["sql"] or "(no query)", language="sql", wrap_lines=True)


# ---- Session history -----------------------------------------------------------

def show_entry(entry_id: int) -> None:
    st.session_state["current_id"] = entry_id
    st.session_state.pop("notice", None)


def use_example(question: str) -> None:
    st.session_state["question"] = question
    st.session_state["submit"] = True


def add_to_history(question: str, out: dict, source_label: str, is_upload: bool, table_names: list[str]) -> None:
    history = st.session_state.setdefault("history", [])
    entry_id = len(history)
    history.append({
        "id": entry_id, "question": question, "out": out, "source_label": source_label,
        "is_upload": is_upload, "tables": tables_used(out["sql"], table_names),
        "time": datetime.datetime.now().strftime("%H:%M"),
    })
    show_entry(entry_id)


# ---- Page --------------------------------------------------------------------

CSS = """
<style>
.block-container { max-width: 1120px; padding-top: 2.4rem; }
.eyebrow { text-transform: uppercase; letter-spacing: .16em; font-size: .7rem; font-weight: 600;
           opacity: .6; margin: 0 0 -.35rem 0; }
h1 { letter-spacing: -0.025em; }
.subtitle { opacity: .78; margin-top: -.35rem; }
[class*="st-key-card-"] { padding: 1.15rem 1.35rem 1.25rem;
    box-shadow: 0 1px 2px rgba(15, 30, 28, .05), 0 8px 24px -16px rgba(15, 30, 28, .25); }
[class*="st-key-card-"] h3 { margin-top: -.2rem; }
[data-testid="stMetricLabel"] p { text-transform: uppercase; letter-spacing: .09em; font-size: .74rem; opacity: .7; }
[data-testid="stMetricValue"] { font-family: "Space Grotesk", sans-serif; letter-spacing: -0.02em; }
.st-key-examples button { border-radius: 999px; padding: .2rem .85rem; min-height: 0; }
.st-key-examples button p { font-size: .85rem; }
.st-key-history button, .st-key-history button > div { justify-content: flex-start; text-align: left; width: 100%; }
.st-key-history button p { font-size: .87rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.empty-state { opacity: .65; text-align: center; padding: 2.2rem 1rem; }
[class*="st-key-answer-text-"] p { font-size: 1.28rem; line-height: 1.55; font-weight: 500; margin-bottom: .3rem; }
[class*="st-key-alternatives-"] button { border-radius: 999px; padding: .2rem .8rem; min-height: 0; }
[class*="st-key-alternatives-"] button p { font-size: .85rem; }
@media (max-width: 640px) {
  .block-container { padding: 3.6rem .9rem 3rem; }  /* clear Streamlit's top bar */
  h1 { font-size: 1.85rem !important; }
  [data-testid="stMetricValue"] { font-size: 2rem; }
  [class*="st-key-card-"] { padding: .9rem .95rem 1rem; }
  [class*="st-key-answer-text-"] p { font-size: 1.12rem; }
}
</style>
"""

st.set_page_config(page_title="Ask a Database", page_icon="🔎", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)

if not os.getenv("ANTHROPIC_API_KEY"):
    st.error("No ANTHROPIC_API_KEY found. Add it to .streamlit/secrets.toml or your .env file.")
    st.stop()

# Sidebar: data source, uploads and the schema browser.
with st.sidebar:
    source = st.selectbox(
        "Data source",
        list(DATABASES) + [UPLOAD],
        format_func=lambda d: "Use my own data (CSV)" if d == UPLOAD else DATABASES[d]["label"],
    )
    upload = load_uploads() if source == UPLOAD else None
    loaded = upload if upload and "conn" in upload and upload["conn"] else None
    if source == UPLOAD:
        schema = {t["name"]: [(c["name"], c["type"]) for c in t["columns"]] for t in loaded["tables"]} if loaded else {}
        source_label = "Your uploaded data"
    else:
        schema = describe_tables(source)
        source_label = DATABASES[source]["label"]
    if schema:
        with st.expander("Schema", icon=":material/table_chart:"):
            for table, columns in schema.items():
                st.markdown(f"**{table}**  \n" + " · ".join(f"`{name}` {ctype.lower()}" for name, ctype in columns))

# A new data source starts with a clean answer area (history is kept).
if st.session_state.get("active_source") != source:
    st.session_state["active_source"] = source
    st.session_state.pop("current_id", None)
    st.session_state.pop("notice", None)

# Header.
left, right = st.columns([5, 1.4], vertical_alignment="bottom")
with left:
    st.markdown('<p class="eyebrow">Text-to-SQL workspace</p>', unsafe_allow_html=True)
    st.title("Ask a Database")
    st.markdown(
        '<p class="subtitle">Ask about data in plain English. Every answer shows the SQL it ran and how it got there.</p>',
        unsafe_allow_html=True,
    )
with right:
    with st.popover("How accurate is this?", type="tertiary", icon=":material/info:"):
        st.markdown("**Execution accuracy on public benchmarks**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Spider dev", "81.4%")
        c2.metric("Spider-Realistic", "77.6%")
        c3.metric("BIRD Mini-Dev", "65%")
        st.write(
            "On typical questions about 4 in 5 answers are exactly right, and it's less reliable on "
            "messier, real-world data, so treat answers as a helpful first draft and check anything important."
        )
        st.caption(
            f"Measured with {MODEL} using the same prompt as this app. "
            "The app also retries a query when it fails or returns nothing."
        )

st.caption(f":material/database: {source_label}")

# Upload status (shown in the main area so it's visible on phones too).
ready = True
if source == UPLOAD:
    if upload and "too_many" in upload:
        st.error(f"Please upload at most {MAX_FILES} files (remove {upload['too_many'] - MAX_FILES} to continue).")
    elif upload:
        for error in upload["errors"]:
            st.error(error)
        if upload["notes"]:  # one compact box for all the "we fixed this" notes
            st.warning("**Notes about your files**\n" + "\n".join(f"- {note}" for note in upload["notes"]),
                       icon=":material/info:")
    else:
        st.info("Upload up to 3 CSV files in the sidebar to start asking questions about them.", icon=":material/upload_file:")
    ready = loaded is not None

# Question bar.
if ready:
    if loaded:
        scope, placeholder = f"upload:{hash(loaded['signature'])}", f"e.g. How many rows are in {loaded['tables'][0]['name']}?"
    else:
        scope, placeholder = source, "e.g. How many singers are there?"
    with st.form("ask", border=False):
        q_col, b_col = st.columns([6, 1], vertical_alignment="bottom")
        question = q_col.text_input("Ask a question", key="question", placeholder=placeholder)
        if b_col.form_submit_button("Ask", type="primary", icon=":material/arrow_forward:", width="stretch"):
            st.session_state["submit"] = True
    if source != UPLOAD:
        with st.container(horizontal=True, gap="small", key="examples"):
            for i, example in enumerate(DATABASES[source]["examples"]):
                st.button(example, key=f"example-{source}-{i}", on_click=use_example, args=(example,))

    if st.session_state.pop("submit", False) and question.strip():
        q = question.strip()
        with st.spinner("Writing and running the query, then writing the answer..."):
            if loaded:
                conn, schema_text = loaded["conn"], loaded["schema"]
                out = ask(
                    scope, q, share=False,
                    compute=lambda: compute_answer(
                        q, DB_NAME, run=lambda sql: run_read_only_connection(sql, conn),
                        schema_text=schema_text, table_columns=schema, schema=schema_text,
                        names=readable_names(schema, loaded),
                    ),
                )
            else:
                out = ask(scope, q, compute=lambda: compute_answer(
                    q, source, run=lambda sql: run_read_only(sql, db_path(source)),
                    schema_text=get_schema_text(source, db_dir=DB_DIR), table_columns=schema,
                    names=readable_names(schema, None), db_dir=DB_DIR,
                ))
        if out.get("limit_reached") or out.get("api_error"):
            st.session_state["notice"] = out
        else:
            add_to_history(q, out, source_label, loaded is not None, list(schema))

# Answer area.
history = st.session_state.get("history", [])
notice = st.session_state.get("notice")
current_id = st.session_state.get("current_id")
if notice and notice.get("limit_reached"):
    st.info(f"This demo has answered its {DAILY_LIMIT} questions for today. Thanks for trying it, and please come back tomorrow!")
elif notice:
    st.error("Sorry, the AI service didn't respond. Please try again in a moment.")
elif current_id is not None:
    render_answer(history[current_id])
elif ready:
    with st.container(border=True, key="card-empty"):
        st.markdown(
            '<div class="empty-state">Your answer will appear here: a headline number or a table, '
            "a chart when it helps, and the SQL behind it.</div>",
            unsafe_allow_html=True,
        )

# Sidebar, continued: this session's questions and the daily counter (drawn last so they're up to date).
with st.sidebar:
    st.markdown("**This session**")
    if history:
        with st.container(key="history", gap=None):
            for entry in reversed(history):
                is_current = entry["id"] == current_id
                st.button(
                    entry["question"], key=f"history-{entry['id']}", on_click=show_entry, args=(entry["id"],),
                    type="tertiary", width="stretch",
                    icon=":material/radio_button_checked:" if is_current else ":material/history:",
                    help=f"{entry['source_label']} · {entry['time']}",
                )
    else:
        st.caption("Questions you ask will appear here.")
    st.caption(f"{max(DAILY_LIMIT - read_usage(), 0)} of {DAILY_LIMIT} questions left today.")
