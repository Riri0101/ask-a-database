"""Turn a query result into a short, plain-English answer, and check its numbers.

Used by the app after the SQL has run (SQL generation itself is in sql_agent.py):
1. collect_facts()  - row counts and date ranges of the tables used (for honest
   statements like "all 26 quarters from 2017 to 2023").
2. write_answer()   - one Claude call that writes the sentence, its assumptions,
   and alternative readings of the question.
3. check_numbers()  - every number in the sentence must match the result, or come
   from the question, the SQL, or the data facts; otherwise we use template_answer().
"""

import json
import math
import re
from typing import Callable

import anthropic

MAX_RESULT_ROWS = 20  # rows of the result shown to the answer writer

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "assumptions", "alternatives"],
    "additionalProperties": False,
}

ANSWER_PROMPT = """You write the final answer to a data question, for a non-technical manager.

Question: {question}

The SQL query that was run:
{sql}

The database (tables, columns, a few sample rows):
{schema}

Facts about the data the query used:
{facts}

The query result has {n_rows} row(s), columns: {columns}
{rows}

Range of each number column over all {n_rows} result rows (computed in code):
{ranges}

Return JSON with:
- "answer": 1 to 2 plain-English sentences that directly answer the question. Use a second sentence only for an assumption the reader needs to know.
  - Describe only what the SQL actually computed. Don't pick, rank or derive values from the result that the query didn't compute itself: never call a value "the latest", "the most recent", "the highest", "the top" and so on unless the SQL sorted by that (ORDER BY) or picked it (MAX, MIN, LIMIT). Rows come back in whatever order the SQL produced; without ORDER BY that order means nothing.
  - If the result is a list of values and the question seems to want a single value, summarize the list honestly using the ranges above, for example "Gross Profit for each of the 26 quarters ranges from 548 to 801" (say "ranges from X to Y", not "highest" or "lowest"), and offer the more specific readings (such as the latest quarter, or the total) as alternatives.
  - Give the key number(s) exactly as they appear in the result, formatted readably: thousands separators (12,345) and sensible rounding (at most 2 decimals). Do not calculate new numbers that aren't in the result (no new sums, differences, averages or percentages).
  - Refer to columns by readable names ("Gross Profit", not "gross_profit").
  - If the data has several similar columns (for example "Gross Profit", "Premium Gross Profit", "Ad Gross Profit"), name the column that was used.
  - If the query made an assumption about which column, which time period or what was included, say it plainly in the sentence (for example "adding up all 26 quarters from March 2017 to March 2023").
  - Units: mention a currency or unit only if a column name or the data makes it clear. Never assume dollars, and don't add words like "million" unless the data says values are in millions. If the number is an amount whose unit is unknown, say "in the units used in your data". Counts need no unit.
  - If the result is empty, say that nothing in the data matched, without inventing numbers.
- "assumptions": a short phrase for each real choice the query made that a reader might disagree with (for example "used Gross Profit, not Premium or Ad Gross Profit", "added up all quarters"). Don't list things that simply follow from the question (such as "no filters applied" or "counted every row"). For a straightforward question, return an empty list.
- "alternatives": only if the question is genuinely ambiguous, meaning it could reasonably refer to a different similar column or a different time period (such as the most recent period instead of the total, or values per period), give 2 to 3 rewritten questions. Each must ask the same thing under that other reading, naming the column or period explicitly (for example "What was the Gross Profit for the latest quarter?"), so that rerunning it makes the query do the work. Don't suggest different or follow-up questions. Most questions are not ambiguous: then return an empty list."""

DATE_LIKE = re.compile(r"date|year|time|period|quarter|month", re.IGNORECASE)


def collect_facts(run: Callable[[str], dict], table_columns: dict[str, list[tuple[str, str]]], tables: list[str]) -> str:
    """Row counts and date ranges for the tables a query used, as text.

    `run(sql)` is the app's read-only query runner; `table_columns` is {table: [(column, type)]}.
    """
    lines = []
    for table in tables:
        count = run(f'SELECT COUNT(*) FROM "{table}"')
        if count["ok"]:
            lines.append(f"Table {table} has {count['rows'][0][0]} rows.")
        for column, ctype in table_columns.get(table, []):
            if ctype.upper() in ("DATE", "DATETIME", "TIMESTAMP") or DATE_LIKE.search(column):
                span = run(f'SELECT MIN("{column}"), MAX("{column}"), COUNT(DISTINCT "{column}") FROM "{table}"')
                if span["ok"] and span["rows"][0][0] is not None:
                    low, high, distinct = span["rows"][0]
                    lines.append(f"Column {table}.{column} runs from {low} to {high} ({distinct} distinct values).")
    return "\n".join(lines) or "(none)"


def column_ranges(columns: list[str], rows: list[tuple]) -> str:
    """"col: from X to Y" for each number column, over every row (the writer only sees 20)."""
    lines = []
    for i, column in enumerate(columns):
        values = [r[i] for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)]
        if len(values) > 1 and len(values) == sum(r[i] is not None for r in rows):
            lines.append(f"{column}: from {min(values)} to {max(values)}")
    return "\n".join(lines) or "(no number columns with more than one value)"


def write_answer(
    question: str, sql: str, columns: list[str], rows: list[tuple], schema: str, facts: str,
    model: str, effort: str = "low",
) -> dict:
    """Ask Claude for {"answer", "assumptions", "alternatives"} (plus token counts). API errors are raised."""
    shown = rows[:MAX_RESULT_ROWS]
    rows_text = "\n".join(str(tuple(r)) for r in shown) or "(no rows)"
    if len(rows) > len(shown):
        rows_text += f"\n(first {len(shown)} of {len(rows)} rows shown)"
    prompt = ANSWER_PROMPT.format(
        question=question, sql=sql, schema=schema, facts=facts,
        n_rows=len(rows), columns=", ".join(columns), rows=rows_text, ranges=column_ranges(columns, rows),
    )
    client = anthropic.Anthropic(max_retries=2)
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    data["alternatives"] = [a for a in data["alternatives"] if a.strip()][:3]
    data["input_tokens"] = response.usage.input_tokens
    data["output_tokens"] = response.usage.output_tokens
    return data


# ---- Number check ---------------------------------------------------------------

SCALES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12}
NUMBER = re.compile(
    r"(?<![\w.,])(?P<sign>[-−])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<word>thousand|million|billion|trillion)\b|(?P<abbr>k|K|M|mn|bn|B)\b)?"
    r"(?P<pct>\s?%|\s?percent\b)?"
)
PERIOD_LABELS = re.compile(r"\b[QH][1-4]\b|\b\d+(?:st|nd|rd|th)\b|\bFY\d{2,4}\b")


def find_numbers(text: str) -> list[dict]:
    """Numbers written in text, with how precisely they were written.

    "4,734.77" -> 4734.77 (2 decimals); "3.04 billion" -> 3.04e9; "45%" -> 45 (percent).
    Period labels such as Q1, FY2023 or 3rd are skipped; ISO dates split into their parts.
    """
    found = []
    for m in NUMBER.finditer(PERIOD_LABELS.sub(" ", text)):
        raw = m.group("num")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        scale_word = (m.group("word") or m.group("abbr") or "").lower()
        scale = SCALES.get(scale_word, 1.0)
        value = float(raw.replace(",", "")) * scale * (-1 if m.group("sign") else 1)
        found.append({
            "text": m.group(0).strip(), "value": value, "scale": scale,
            "tolerance": 0.5 * 10 ** -decimals * scale, "percent": bool(m.group("pct")),
        })
    return found


def _numeric_cells(rows: list[tuple]) -> list[float]:
    values = []
    for row in rows:
        for cell in row:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float)) and math.isfinite(cell):
                values.append(float(cell))
            elif isinstance(cell, str):  # numbers inside text cells, e.g. "44.7%" or dates
                values += [n["value"] for n in find_numbers(cell)]
    return values


def _matches(number: dict, value: float) -> bool:
    """Does a written number match a result value, allowing for rounding and sign?"""
    readings = [(number["value"], number["tolerance"])]
    if number["percent"]:  # "45%" may be stored as 45 or as 0.45
        readings.append((number["value"] / 100, number["tolerance"] / 100))
    for x, tolerance in readings:
        # Compare sizes, so "a loss of 10" matches -10.
        if abs(abs(x) - abs(value)) <= tolerance + 1e-9 * max(1.0, abs(value)):
            return True
    return False


def check_numbers(text: str, rows: list[tuple], allowed_texts: list[str]) -> tuple[bool, list[str]]:
    """Check every number in `text` against the result rows.

    A number passes if it matches a result value (within the rounding it was written
    with), or appears in one of `allowed_texts` (the question, the SQL, the data facts),
    or is the number of result rows. Returns (passed, numbers that didn't match).
    """
    results = _numeric_cells(rows)
    context = {round(n["value"], 9) for t in allowed_texts for n in find_numbers(t)}
    context.add(float(len(rows)))
    unmatched = []
    for number in find_numbers(text):
        if number["scale"] == 1.0 and round(number["value"], 9) in context:
            continue
        if not any(_matches(number, v) for v in results):
            unmatched.append(number["text"])
    return not unmatched, unmatched


# ---- Template answer --------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return "no value"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _units_note(label: str) -> str:
    """Counts need no unit; other amounts get the honest "units used in your data"."""
    return "" if re.match(r"(count|number of)\b", label, re.IGNORECASE) else " in the units used in your data"


def template_answer(columns: list[str], rows: list[tuple], labels: Callable[[str], str]) -> str:
    """A plain answer built straight from the result, used when the model's sentence fails a check.

    A list of numbers is summarized honestly as a range, without picking any one value.
    """
    if not rows:
        return "The query ran, but nothing in the data matched this question."
    if len(rows) == 1:
        parts = [f"{labels(c)}: {_fmt(v)}" for c, v in zip(columns, rows[0])]
        number_labels = [labels(c) for c, v in zip(columns, rows[0]) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return "; ".join(parts) + (_units_note(number_labels[0]) if len(number_labels) == 1 else "") + "."
    numeric = [
        i for i in range(len(columns))
        if all(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in rows if r[i] is not None)
        and any(r[i] is not None for r in rows)
    ]
    if len(numeric) == 1:
        i = numeric[0]
        values = [r[i] for r in rows if r[i] is not None]
        label = labels(columns[i])
        return (f"The query returned {label} for {len(rows):,} rows, ranging from "
                f"{_fmt(min(values))} to {_fmt(max(values))}{_units_note(label)}.")
    return f"The query returned {len(rows):,} rows, shown in the table below."


# ---- Ranking words ------------------------------------------------------------------

RANKING_WORDS = re.compile(
    r"\b(latest|most recent|newest|oldest|earliest|highest|lowest|largest|smallest|biggest|"
    r"top|bottom|maximum|minimum|peak|best|worst|"
    r"(?<!\bat )most|(?<!\bat )least)\b",  # "at least one" / "at most 3" aren't ranking claims
    re.IGNORECASE,
)
PICKS_IN_SQL = re.compile(r"\border\s+by\b|\bmax\s*\(|\bmin\s*\(|\blimit\b", re.IGNORECASE)


def unsupported_ranking(text: str, sql: str) -> list[str]:
    """Ranking words ("latest", "highest", ...) in the text that the SQL doesn't back up.

    Such a word is only allowed if the query itself sorted or picked (ORDER BY, MAX, MIN, LIMIT).
    """
    if PICKS_IN_SQL.search(sql or ""):
        return []
    return sorted({m.group(0).lower() for m in RANKING_WORDS.finditer(text)})


def use_readable_names(text: str, names: dict[str, str]) -> str:
    """Replace raw column names ("gross_profit") with readable ones ("Gross Profit")."""
    for raw in sorted(names, key=len, reverse=True):  # longest first, so premium_gross_profit beats gross_profit
        text = re.sub(rf"(?<![\w.]){re.escape(raw)}(?!\w)", names[raw], text, flags=re.IGNORECASE)
    return text
