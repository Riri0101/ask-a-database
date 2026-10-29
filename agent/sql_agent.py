import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

import anthropic
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from agent.schema import get_db_path, get_schema_text

load_dotenv()

# Free-tier Gemini Flash models, tried in order when no model is given (for the app).
# Free-tier models are sometimes overloaded (error 503), so if one is busy we fall
# back to the next. Set GEMINI_MODEL in your .env file to try a specific model first.
MODEL_NAMES = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-flash-latest"]
if os.getenv("GEMINI_MODEL"):
    MODEL_NAMES.insert(0, os.getenv("GEMINI_MODEL"))

# Room for Claude's (hidden) thinking plus the SQL itself.
CLAUDE_MAX_TOKENS = 16000


# Agent versions, so every change can be measured against the one before it.
# See evaluation/results/version_log.md for what each one scored.
BASE_RULES = """- Use only the tables and columns listed above.
- Return ONLY the SQL query: no explanation, no markdown, no code fences."""

# Rules aimed at the mistakes seen on the train100 development set.
# General rules: good for any text-to-SQL use.
GENERAL_RULES = """
- Return exactly the columns the question asks for, in the order the question mentions them. Do not add columns that were not asked for (such as ids or counts), and do not use SELECT *.
- Do not reformat values: no string concatenation, CAST, REPLACE, TRIM or ROUND, and no added symbols or units, unless the question asks for it. For a "full name", return the first-name and last-name columns as separate columns.
- Write literal values exactly as they appear in the sample rows (same spelling, capitalization and format). For example, if the sample rows show 'english', write 'english', not 'English'."""
# Spider-convention rules: match how Spider's answers are written, which may not
# be what a real user wants (e.g. they might prefer duplicates removed).
SPIDER_RULES = """
- Do not add DISTINCT unless the question asks for distinct, different or unique values.
- Use inner joins. Only use LEFT JOIN if the question asks to include rows that have no match."""

# Agent versions, so every change can be measured against the one before it.
# See evaluation/results/version_log.md for what each one scored.
# Each version = the prompt rules it uses + whether it self-corrects.
VERSION_RULES = {
    "v1": BASE_RULES,
    "v2": BASE_RULES + GENERAL_RULES + SPIDER_RULES,  # same prompt as v2b
    "v2a": BASE_RULES + GENERAL_RULES,
    "v2b": BASE_RULES + GENERAL_RULES + SPIDER_RULES,
    "v3": BASE_RULES + GENERAL_RULES + SPIDER_RULES,  # v2 + self-correction
    "v3a": BASE_RULES + GENERAL_RULES,  # v2a + self-correction
    "v4": BASE_RULES + GENERAL_RULES,  # v2a + robust SQL extraction
    "v4b": BASE_RULES + GENERAL_RULES + SPIDER_RULES,  # v4 + Spider-convention rules
    "v4sc": BASE_RULES + GENERAL_RULES,  # v4 + self-correction (used by the Streamlit app)
}
SELF_CORRECTING_VERSIONS = {"v3", "v3a", "v4sc"}
# These versions use extract_sql, which ignores any text before the query.
ROBUST_EXTRACT_VERSIONS = {"v4", "v4b", "v4sc"}
AGENT_VERSIONS = list(VERSION_RULES)
LATEST_VERSION = "v4"  # default for the evaluation and the app (the official headline version)
# Self-correction: if the SQL errors or returns no rows, show the model what
# happened and let it fix the query, up to this many times.
MAX_FIXES = 2


def _hint_section(hint: str | None) -> str:
    """Extra knowledge for the question (BIRD's "evidence"); empty when there is none."""
    return f"\n\nHint:\n{hint.strip()}" if hint and hint.strip() else ""


def build_prompt(question: str, schema: str, version: str = LATEST_VERSION, hint: str | None = None) -> str:
    """Put the schema and the question into one set of instructions for the model.

    Without a hint, the prompt is exactly the same as before hints existed.
    """
    if version not in VERSION_RULES:
        raise ValueError(f"Unknown agent version {version!r}; choose from {AGENT_VERSIONS}")
    rules = VERSION_RULES[version]
    return f"""You are an expert at writing SQLite queries.

Here is the database you will query:

{schema}

Write one SQLite query that answers this question:
{question}{_hint_section(hint)}

Rules:
{rules}"""


def build_fix_prompt(
    question: str, schema: str, sql: str, problem: str, version: str, hint: str | None = None
) -> str:
    """Ask the model to repair a query, given what went wrong when it ran.

    Uses the same rules as the version's first prompt.
    """
    rules = VERSION_RULES[version]
    return f"""You are an expert at writing SQLite queries.

Here is the database you will query:

{schema}

Question:
{question}{_hint_section(hint)}

You previously wrote this query:
{sql}

{problem}

Write a corrected SQLite query that answers the question.

Rules:
{rules}"""


def clean_sql(text: str) -> str:
    """Strip markdown code fences or stray text the model may add around the SQL."""
    text = text.strip()
    # If the model wrapped the answer in ```sql ... ```, keep only what's inside.
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    return text


# Where a query starts: a line beginning with SELECT, or a real CTE ("WITH name AS (").
# The CTE pattern is strict so prose like "With that in mind, ..." doesn't match.
_SQL_START = re.compile(
    r"^[ \t]*(?:SELECT\b|WITH\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\))?\s+AS\s*\()",
    re.IGNORECASE | re.MULTILINE,
)


def extract_sql(text: str) -> str:
    """Like clean_sql, but also drop any text the model writes before the query (v4+).

    Looks for the first line that starts with SELECT or a WITH ... AS ( clause; if
    there is none, falls back to the first upper-case SELECT anywhere in the text.
    """
    text = clean_sql(text)
    start = _SQL_START.search(text) or re.search(r"\bSELECT\b", text)
    return text[start.start():].strip() if start else text


def _call_claude(prompt: str, model: str, effort: str) -> dict:
    """One Claude request. API errors (anthropic.APIStatusError etc.) go to the caller."""
    # max_retries=0: the caller decides when to wait and retry.
    client = anthropic.Anthropic(max_retries=0)
    response = client.messages.create(
        model=model,
        max_tokens=CLAUDE_MAX_TOKENS,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
    )
    # The reply can contain "thinking" blocks as well as text; we only want the text.
    text = "".join(block.text for block in response.content if block.type == "text")
    return {
        "text": text,
        "sql": clean_sql(text),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,  # includes thinking tokens
        "stop_reason": response.stop_reason,
    }


def _call_gemini(prompt: str, model: str) -> dict:
    """One Gemini request. API errors (google.genai.errors.APIError) go to the caller."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        raise ValueError("Set GEMINI_API_KEY in your .env file.")
    # One attempt only: the SDK otherwise retries ~1s apart, which would break
    # the caller's own rate-limit pacing.
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )
    response = client.models.generate_content(model=model, contents=prompt)
    usage = response.usage_metadata
    return {
        "text": response.text or "",
        "sql": clean_sql(response.text or ""),
        "input_tokens": usage.prompt_token_count or 0,
        "output_tokens": (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
        "stop_reason": str(response.candidates[0].finish_reason) if response.candidates else "",
    }


def _ask_model(prompt: str, model: str, effort: str, version: str) -> dict:
    """Send one prompt to the provider picked from the model name."""
    if model.startswith("claude"):
        result = _call_claude(prompt, model, effort)
    elif model.startswith("gemini"):
        result = _call_gemini(prompt, model)
    else:
        raise ValueError(f"Unknown model provider for {model!r} (expected claude-... or gemini-...)")
    if version in ROBUST_EXTRACT_VERSIONS:
        result["sql"] = extract_sql(result["text"])
    return result


def generate_sql_with_usage(
    question: str,
    db_name: str,
    model: str,
    effort: str = "medium",
    version: str = LATEST_VERSION,
    hint: str | None = None,
    db_dir: Path | None = None,
    schema: str | None = None,
    run: Callable[[str], dict] | None = None,
) -> dict:
    """Ask one specific model for SQL and report the tokens it used.

    The provider is picked from the model name ("claude-..." or "gemini-...").
    `effort` applies to Claude only; `version` picks the agent version.
    `hint` is optional extra knowledge (BIRD's evidence); `db_dir` is the folder of
    databases (default: Spider).
    For a database that isn't a file (e.g. uploaded CSVs held in memory), pass the
    `schema` text and a `run(sql) -> {"ok", "rows", ...}` function for self-correction.
    Returns {"sql", "input_tokens", "output_tokens", "stop_reason", "attempts"},
    where attempts is the number of model calls made. API errors are raised.
    """
    if schema is None:
        schema = get_schema_text(db_name, db_dir=db_dir)
    if run is None:
        run = lambda sql: run_sql(sql, db_name, db_dir=db_dir)  # noqa: E731
    result = _ask_model(build_prompt(question, schema, version, hint), model, effort, version)
    result["attempts"] = 1
    if version not in SELF_CORRECTING_VERSIONS:
        return result

    for _ in range(MAX_FIXES):
        sql = result["sql"]
        if not sql:
            problem = "That reply contained no SQL query."
        else:
            outcome = run(sql)
            if outcome["ok"] and outcome["rows"]:
                break  # it runs and returns rows: nothing to fix
            if outcome["ok"]:
                problem = (
                    "Running it returned no rows. Check the tables, joins and filter values "
                    "(compare them with the sample rows). If you are sure an empty result is "
                    "the correct answer, return the same query unchanged."
                )
            else:
                problem = f"Running it gave this error:\n{outcome['error']}"

        fix_prompt = build_fix_prompt(question, schema, sql, problem, version, hint)
        fix = _ask_model(fix_prompt, model, effort, version)
        result["attempts"] += 1
        result["input_tokens"] += fix["input_tokens"]
        result["output_tokens"] += fix["output_tokens"]
        result["stop_reason"] = fix["stop_reason"]
        if fix["sql"] == sql:
            break  # the model stands by its query (e.g. the empty result is correct)
        result["sql"] = fix["sql"]
    return result


def generate_sql(question: str, db_name: str, model: str | None = None) -> str:
    """Turn a natural-language question into a SQL query for a Spider database.

    With `model`, only that model is used and any API error is raised.
    Without it, the Gemini MODEL_NAMES are tried in order (for the app).
    """
    if model:
        return generate_sql_with_usage(question, db_name, model)["sql"]

    last_error = None
    for model_name in MODEL_NAMES:
        try:
            return generate_sql_with_usage(question, db_name, model_name)["sql"]
        except (errors.ServerError, errors.ClientError) as e:
            # 5xx = model overloaded, 429 = rate limit, 404 = model retired. Try the next one.
            if isinstance(e, errors.ClientError) and e.code not in (404, 429):
                raise
            last_error = e
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def run_sql(sql: str, db_name: str, timeout_seconds: float = 30, db_dir: Path | None = None) -> dict:
    """Run a query on a database (default folder: the Spider databases).

    Returns {"ok": True, "columns": [...], "rows": [...]} on success,
    or {"ok": False, "error": "message"} if anything goes wrong
    (including a query that runs longer than timeout_seconds).
    The database is opened read-only, so a query can never change it.
    """
    try:
        conn = sqlite3.connect(get_db_path(db_name, db_dir).resolve().as_uri() + "?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        # SQLite calls this every 10,000 steps; returning True cancels the query.
        deadline = time.monotonic() + timeout_seconds
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            columns = [col[0] for col in cursor.description] if cursor.description else []
            return {"ok": True, "columns": columns, "rows": rows}
        finally:
            conn.close()
    except (sqlite3.Error, FileNotFoundError) as e:
        return {"ok": False, "error": str(e)}
