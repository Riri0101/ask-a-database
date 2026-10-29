"""Measure the agent's execution accuracy on Spider, Spider-Realistic and BIRD Mini-Dev.

Run from the project root:
    python evaluation/evaluate.py                          # first 50 dev questions
    python evaluation/evaluate.py --limit 200              # first 200 (reuses the 50 already done)
    python evaluation/evaluate.py --limit all              # the whole dev set (official score)
    python evaluation/evaluate.py --split train --limit 100  # train questions, for development
    python evaluation/evaluate.py --split train100 --agent-version v2  # the fixed development set
    python evaluation/evaluate.py --split realistic --limit all  # Spider-Realistic (508 questions)
    python evaluation/evaluate.py --split bird --limit all       # BIRD Mini-Dev with hints (--no-hints to drop them)

Results go to evaluation/results/, one CSV per model (and effort, for Claude) and split,
one row per question, saved after every question. Re-running the same command
skips questions that are already in the CSV, so a stopped run picks up where it left off.

For Gemini models, API calls are spaced SECONDS_BETWEEN_CALLS apart to stay under
the free-tier per-minute limit; if the daily limit is hit, the run stops and can
resume tomorrow. Claude calls are not spaced out; 429s are still retried.
"""

import argparse
import csv
import itertools
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # so `import agent` works however the script is started

import anthropic  # noqa: E402
from google.genai import errors  # noqa: E402

from agent.sql_agent import AGENT_VERSIONS, LATEST_VERSION, generate_sql_with_usage, run_sql  # noqa: E402
from evaluation.hardness import spider_hardness  # noqa: E402

# ---- Settings ---------------------------------------------------------------
MODEL = "claude-haiku-5-5"  # one fixed model for the whole run ("claude-..." or "gemini-...")
EFFORT = "medium"  # Claude only: low | medium | high | xhigh | max (fixed for consistent results)
SEED = 42  # fixes the question order; change it to get a different sample
DEFAULT_LIMIT = 50  # for dev / train; train100 always runs its whole fixed set
# The train100 development set: a fixed 100 train questions, weighted toward the
# hard and extra questions the agent gets wrong most often.
TRAIN100_QUOTAS = {"easy": 15, "medium": 25, "hard": 30, "extra": 30}
USE_HINTS = True  # BIRD only: include each question's "evidence" hint in the prompt (or pass --no-hints)
RETRY_WAITS = [15, 30, 60, 120, 240]  # seconds to wait before each retry on 429/5xx
INPUT_PRICE_PER_M = 0.10  # dollars per million input tokens
OUTPUT_PRICE_PER_M = 0.50  # dollars per million output tokens
# -----------------------------------------------------------------------------

IS_CLAUDE = MODEL.startswith("claude")
# Gemini free tier allows 5 requests/minute per model (one per 12s); Claude needs no pause.
SECONDS_BETWEEN_CALLS = 0 if IS_CLAUDE else 13

SPIDER_DIR = PROJECT_ROOT / "data" / "spider"
BIRD_DIR = PROJECT_ROOT / "data" / "MINIDEV"
# Each split: where its questions come from, where its databases live, and its difficulty labels.
# "dev" is for official scoring; "train" and "train100" are for development, so dev stays unseen.
# "realistic" (Spider-Realistic) and "bird" (BIRD Mini-Dev) are extra test sets.
SPIDER_LEVELS = ["easy", "medium", "hard", "extra"]
BIRD_LEVELS = ["simple", "moderate", "challenging"]
SPLITS = {
    "dev": {"source": SPIDER_DIR / "dev.json", "db_dir": SPIDER_DIR / "database", "levels": SPIDER_LEVELS},
    "train": {"source": SPIDER_DIR / "train_spider.json", "db_dir": SPIDER_DIR / "database", "levels": SPIDER_LEVELS},
    "train100": {"source": SPIDER_DIR / "train_spider.json", "db_dir": SPIDER_DIR / "database", "levels": SPIDER_LEVELS},
    # Spider-Realistic: dev questions reworded to avoid naming columns; uses the Spider dev databases.
    "realistic": {"source": SPIDER_DIR / "spider-realistic.json", "db_dir": SPIDER_DIR / "database", "levels": SPIDER_LEVELS},
    # BIRD Mini-Dev: questions from Hugging Face (the canonical, updated version), databases from data/MINIDEV.
    "bird": {"source": "birdsql/bird_mini_dev:mini_dev_sqlite", "db_dir": BIRD_DIR / "dev_databases", "levels": BIRD_LEVELS},
}
# Seconds a query may run before it is cancelled (same limit for the gold and the agent's SQL).
# BIRD's databases are much larger: two of its gold queries take about 50s and 220s.
QUERY_TIMEOUTS = {"bird": 300}
DEFAULT_QUERY_TIMEOUT = 30
RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"
FIELDS = [
    "split",
    "dev_index",  # position of the example in its split's JSON file
    "question",
    "hint",  # BIRD's evidence, if it was given to the model
    "db_id",
    "difficulty",
    "predicted_sql",
    "gold_sql",
    "model",
    "effort",
    "agent_version",
    "attempts",  # model calls for this question (more than 1 when self-correcting)
    "correct",
    "correct_strict",
    "error",
    "gold_empty",
    "gold_seconds",  # how long the gold query took to run
    "pred_seconds",  # how long the agent's query took to run
    "input_tokens",
    "output_tokens",
]


class ApiGaveUp(Exception):
    """The model API kept failing, so the run should stop (and be resumed later)."""


class DailyLimitReached(ApiGaveUp):
    """The free-tier requests-per-day quota is used up; retrying today won't help."""


_last_call_time = 0.0  # when we last called the API (time.monotonic() seconds)


def wait_for_rate_limit() -> None:
    """Sleep until at least SECONDS_BETWEEN_CALLS have passed since the last API call."""
    global _last_call_time
    remaining = _last_call_time + SECONDS_BETWEEN_CALLS - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    _last_call_time = time.monotonic()


def classify_api_error(e: Exception) -> str | None:
    """Sort an exception into 'daily', 'retry' or 'fatal' (None if it isn't an API error)."""
    if isinstance(e, errors.APIError):  # Gemini
        # A 429 whose quota id mentions 'PerDay' means the daily quota is exhausted;
        # per-minute 429s say 'PerMinute' instead, and those are worth retrying.
        if e.code == 429 and "PerDay" in str(e.details):
            return "daily"
        return "retry" if e.code == 429 or (e.code or 0) >= 500 else "fatal"
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return "retry"
    if isinstance(e, anthropic.APIStatusError):  # Claude; 529 = overloaded
        return "retry" if e.status_code >= 500 else "fatal"
    return None


def has_top_level_order_by(sql: str) -> bool:
    """True if the query's outermost SELECT has ORDER BY (not just a subquery).

    Used for BIRD, which has no pre-parsed SQL. Skips text inside quotes and parentheses.
    """
    depth, quote, upper = 0, None, sql.upper()
    for i, ch in enumerate(sql):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and re.match(r"ORDER\s+BY\b", upper[i:]) and (i == 0 or not upper[i - 1].isalnum()):
            return True
    return False


def load_examples(split: str) -> list[dict]:
    """Load a split and convert every example to one common shape:
    {"question", "db_id", "gold_sql", "difficulty", "ordered", "hint"}.

    "ordered" says whether the gold query's result order matters (top-level ORDER BY).
    """
    source = SPLITS[split]["source"]
    if split == "bird":
        from datasets import load_dataset  # only needed for BIRD

        name, hf_split = source.split(":")
        return [
            {
                "question": ex["question"],
                "db_id": ex["db_id"],
                "gold_sql": ex["SQL"],
                "difficulty": ex["difficulty"],  # BIRD's own labels
                "ordered": has_top_level_order_by(ex["SQL"]),
                "hint": ex["evidence"],
            }
            for ex in load_dataset(name, split=hf_split)
        ]
    return [
        {
            "question": ex["question"],
            "db_id": ex["db_id"],
            "gold_sql": ex["query"],
            "difficulty": spider_hardness(ex["sql"]),
            "ordered": bool(ex["sql"]["orderBy"]),
            "hint": "",
        }
        for ex in json.loads(source.read_text())
    ]


def missing_databases(split: str, examples: list[dict]) -> dict[str, int]:
    """Return {db_id: number of questions} for questions whose database file doesn't exist."""
    db_dir = SPLITS[split]["db_dir"]
    missing = Counter(ex["db_id"] for ex in examples if not (db_dir / ex["db_id"] / f"{ex['db_id']}.sqlite").exists())
    return dict(missing)


def pick_questions(split: str, examples: list[dict], limit: int | None) -> list[tuple[int, dict]]:
    """Shuffle a split with a fixed seed and return the first `limit` examples (all if None).

    The same seed always gives the same order, and a bigger limit just extends
    the list, so --limit 100 includes the same 50 questions as --limit 50.
    """
    order = list(range(len(examples)))
    random.Random(SEED).shuffle(order)
    if split == "train100":
        # Walk the shuffled order, taking questions until each difficulty's quota is full.
        taken = {level: 0 for level in TRAIN100_QUOTAS}
        picked = []
        for i in order:
            level = examples[i]["difficulty"]
            if taken[level] < TRAIN100_QUOTAS[level]:
                taken[level] += 1
                picked.append(i)
        order = picked
    return [(i, examples[i]) for i in order[:limit]]


def generate_with_retry(question: str, db_id: str, version: str, hint: str, db_dir: Path) -> dict:
    """Ask the model for SQL, waiting and retrying if it is busy or rate-limited."""
    for attempt, wait in enumerate([0] + RETRY_WAITS):
        if wait:
            print(f"    model busy, waiting {wait}s (retry {attempt}/{len(RETRY_WAITS)})")
            time.sleep(wait)
        wait_for_rate_limit()
        try:
            return generate_sql_with_usage(
                question, db_id, model=MODEL, effort=EFFORT, version=version, hint=hint, db_dir=db_dir
            )
        except Exception as e:
            kind = classify_api_error(e)
            if kind is None:
                raise
            if kind == "daily":
                raise DailyLimitReached(f"daily free-tier limit reached for {MODEL}") from e
            if kind == "fatal":
                raise ApiGaveUp(f"API error: {e}") from e
            last_error = e
    raise ApiGaveUp(f"still failing after {len(RETRY_WAITS)} retries: {last_error}")


def results_match_strict(pred_rows: list, gold_rows: list, ordered: bool) -> bool:
    """Compare query results exactly, including column order.

    Row order only matters if the gold SQL has ORDER BY.
    """
    if ordered:
        return pred_rows == gold_rows
    # Counter counts how often each row appears, so duplicates still have to match.
    return Counter(pred_rows) == Counter(gold_rows)


def results_match(pred_rows: list, gold_rows: list, ordered: bool) -> bool:
    """Compare query results, ignoring column order (like the Spider test-suite evaluation).

    `SELECT name, age` and `SELECT age, name` answer the same question, so we
    try to line up the predicted columns with the gold columns:
      1. For each gold column, the candidates are the predicted columns that hold
         the same *set* of values.
      2. Try every one-to-one assignment of candidates; reorder the predicted
         rows to match, then apply the usual strict rules (row order only if the
         gold SQL has ORDER BY, duplicates must match).
    The number of columns must still be equal, so extra or missing columns fail.
    """
    if not pred_rows and not gold_rows:
        return True
    if len(pred_rows) != len(gold_rows) or len(pred_rows[0]) != len(gold_rows[0]):
        return False

    num_cols = len(gold_rows[0])
    gold_sets = [{row[i] for row in gold_rows} for i in range(num_cols)]
    pred_sets = [{row[i] for row in pred_rows} for i in range(num_cols)]
    candidates = [[j for j in range(num_cols) if pred_sets[j] == gold_set] for gold_set in gold_sets]

    for perm in itertools.product(*candidates):
        if len(set(perm)) != num_cols:  # each predicted column may be used once
            continue
        reordered = [tuple(row[j] for j in perm) for row in pred_rows]
        if results_match_strict(reordered, gold_rows, ordered):
            return True
    return False


def evaluate_one(split: str, index: int, example: dict, version: str, use_hints: bool) -> dict:
    """Run the agent on one example (in the common shape) and return its CSV row."""
    question, db_id, gold_sql = example["question"], example["db_id"], example["gold_sql"]
    hint = example["hint"] if use_hints else ""
    db_dir = SPLITS[split]["db_dir"]
    answer = generate_with_retry(question, db_id, version, hint, db_dir)
    predicted_sql = answer["sql"]

    timeout = QUERY_TIMEOUTS.get(split, DEFAULT_QUERY_TIMEOUT)
    start = time.monotonic()
    gold = run_sql(gold_sql, db_id, timeout_seconds=timeout, db_dir=db_dir)
    gold_seconds = time.monotonic() - start
    start = time.monotonic()
    pred = run_sql(predicted_sql, db_id, timeout_seconds=timeout, db_dir=db_dir) if predicted_sql else None
    pred_seconds = time.monotonic() - start

    correct, correct_strict, error = False, False, ""
    if not gold["ok"]:
        error = f"gold SQL failed: {gold['error']}"
    elif pred is None:
        error = f"model returned no SQL (stop_reason={answer['stop_reason']})"
    elif not pred["ok"]:
        error = pred["error"]
    else:
        correct = results_match(pred["rows"], gold["rows"], example["ordered"])
        correct_strict = results_match_strict(pred["rows"], gold["rows"], example["ordered"])

    return {
        "split": split,
        "dev_index": index,
        "question": question,
        "hint": hint,
        "db_id": db_id,
        "difficulty": example["difficulty"],
        "predicted_sql": predicted_sql,
        "gold_sql": gold_sql,
        "model": MODEL,
        "effort": EFFORT if IS_CLAUDE else "",
        "agent_version": version,
        "attempts": answer["attempts"],
        "correct": correct,
        "correct_strict": correct_strict,
        "error": error,
        "gold_empty": gold["ok"] and not gold["rows"],
        "gold_seconds": round(gold_seconds, 2),
        "pred_seconds": round(pred_seconds, 2),
        "input_tokens": answer["input_tokens"],
        "output_tokens": answer["output_tokens"],
    }


def load_done(csv_path: Path) -> dict[int, dict]:
    """Read rows saved by earlier runs, keyed by dev_index.

    If the file was written with older columns, rewrite it with the current
    ones (new columns left blank) so appended rows line up.
    """
    if not csv_path.exists():
        return {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        old_fields = reader.fieldnames
    if old_fields != FIELDS:
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, restval="")
            writer.writeheader()
            writer.writerows({k: row.get(k, "") for k in FIELDS} for row in rows)
    return {int(row["dev_index"]): row for row in rows}


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} = {100 * part / whole:.1f}%" if whole else "0/0"


def print_summary(rows: list[dict], total: int, split: str, version: str) -> None:
    # Rows read back from the CSV hold text, so compare against the string "True".
    is_correct = [r["correct"] in (True, "True") for r in rows]
    is_strict = [r.get("correct_strict") in (True, "True") for r in rows]
    is_empty = [r["gold_empty"] in (True, "True") for r in rows]
    input_tokens = sum(int(r.get("input_tokens") or 0) for r in rows)
    output_tokens = sum(int(r.get("output_tokens") or 0) for r in rows)
    cost = input_tokens / 1e6 * INPUT_PRICE_PER_M + output_tokens / 1e6 * OUTPUT_PRICE_PER_M

    print("\n" + "=" * 50)
    effort_note = f"   Effort: {EFFORT}" if IS_CLAUDE else ""
    print(f"Model: {MODEL}{effort_note}   Agent: {version}   Split: {split}   Questions evaluated: {len(rows)}/{total}")
    print(f"Execution accuracy:                 {pct(sum(is_correct), len(rows))}")
    print(f"Strict (column order must match):   {pct(sum(is_strict), len(rows))}")
    print(f"\nBy difficulty:    {'execution accuracy':<24}strict")
    for level in SPLITS[split]["levels"]:
        level_ok = [ok for r, ok in zip(rows, is_correct) if r["difficulty"] == level]
        level_strict = [ok for r, ok in zip(rows, is_strict) if r["difficulty"] == level]
        print(f"  {level:<11}     {pct(sum(level_ok), len(level_ok)):<24}{pct(sum(level_strict), len(level_strict))}")
    print(f"\nErrored (SQL failed to run or no SQL): {sum(1 for r in rows if r['error'])}")
    lucky = sum(1 for ok, empty in zip(is_correct, is_empty) if ok and empty)
    print(f"Correct, but correct result was empty:  {lucky}  (could match by luck)")

    timeout = QUERY_TIMEOUTS.get(split, DEFAULT_QUERY_TIMEOUT)
    if timeout > DEFAULT_QUERY_TIMEOUT:
        # Score as if queries had the usual limit: anything slower counts as wrong.
        def too_slow(r: dict) -> bool:
            return max(float(r.get("gold_seconds") or 0), float(r.get("pred_seconds") or 0)) > DEFAULT_QUERY_TIMEOUT

        slow = [r["dev_index"] for r in rows if too_slow(r)]
        within = sum(1 for r, ok in zip(rows, is_correct) if ok and not too_slow(r))
        print(f"\nQuery limit used: {timeout}s. Under a {DEFAULT_QUERY_TIMEOUT}s limit instead:")
        print(f"  Execution accuracy:               {pct(within, len(rows))}")
        print(f"  Questions with a query over {DEFAULT_QUERY_TIMEOUT}s:  {len(slow)}  {slow}")
    print(f"\nTokens: {input_tokens:,} input, {output_tokens:,} output")
    print(
        f"Estimated cost: ${cost:.4f}  "
        f"(at ${INPUT_PRICE_PER_M:.2f} / ${OUTPUT_PRICE_PER_M:.2f} per million input / output tokens)"
    )
    print("=" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--split",
        choices=list(SPLITS),
        default="dev",
        help="dev (official scoring), train, train100 (fixed 100-question development set), "
        "realistic (Spider-Realistic) or bird (BIRD Mini-Dev)",
    )
    parser.add_argument("--limit", help=f'number of questions, or "all" (default {DEFAULT_LIMIT}; all for train100)')
    parser.add_argument("--agent-version", choices=AGENT_VERSIONS, default=LATEST_VERSION, help="agent version to run")
    parser.add_argument("--run-tag", default="", help='extra label for a repeat run, e.g. "run2" (writes a separate CSV)')
    parser.add_argument("--no-hints", action="store_true", help="BIRD: leave the evidence hints out of the prompt")
    args = parser.parse_args()
    if args.limit is None:
        args.limit = "all" if args.split == "train100" else str(DEFAULT_LIMIT)
    limit = None if args.limit == "all" else int(args.limit)
    version = args.agent_version
    use_hints = USE_HINTS and not args.no_hints

    examples = load_examples(args.split)
    missing = missing_databases(args.split, examples)
    if missing:
        print(f"Missing databases in {SPLITS[args.split]['db_dir']} (db_id: questions): {missing}")
        sys.exit(1)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    model_part = f"{MODEL}_effort-{EFFORT}" if IS_CLAUDE else MODEL
    hint_part = "_nohints" if args.split == "bird" and not use_hints else ""
    tag_part = f"_{args.run_tag}" if args.run_tag else ""
    csv_path = RESULTS_DIR / f"{model_part}_{version}_{args.split}{hint_part}_seed{SEED}{tag_part}.csv"
    done = load_done(csv_path)
    questions = pick_questions(args.split, examples, limit)
    print(f"Results file: {csv_path}")
    print(f"{sum(i in done for i, _ in questions)} of {len(questions)} questions already done")

    write_header = not csv_path.exists()
    stopped_early = False
    with csv_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()

        for n, (index, example) in enumerate(questions, start=1):
            if index in done:
                continue
            try:
                row = evaluate_one(args.split, index, example, version, use_hints)
            except DailyLimitReached as e:
                print(f"\nStopped: {e}.")
                print(f"Progress is saved ({len(done)} questions in {csv_path.name}).")
                print("Resume tomorrow by running the same command again.")
                stopped_early = True
                break
            except ApiGaveUp as e:
                print(f"\nStopped: {e}\nRun the same command again later to resume.")
                stopped_early = True
                break
            writer.writerow(row)
            f.flush()  # write to disk now, so nothing is lost if the run stops
            done[index] = row
            mark = "OK " if row["correct"] else "ERR" if row["error"] else "X  "
            print(f"[{n}/{len(questions)}] {mark} {row['difficulty']:<11} {args.split}#{index}: {example['question']}")

    print_summary([done[i] for i, _ in questions if i in done], len(questions), args.split, version)
    if stopped_early:
        sys.exit(1)


if __name__ == "__main__":
    main()
