# Ask a Database

An AI agent that answers business questions about a database in plain English, measured on 2,000+ benchmark questions and built to show its work.

**Live demo:** _coming soon_ <!-- add the deployed app link here -->

---

## The question this project answers

**How reliable is AI at answering business questions from a database, and what makes it more reliable?**

Anyone can connect a language model to a database and get answers that sound right. The harder part is knowing how often they *are* right, and which changes actually improve that. This project treats the agent like a product experiment: a fixed baseline, one change at a time, measured on held-out test sets, with every result logged.

---

## Results

The agent uses **Claude Haiku 5.5** (effort: medium). Accuracy is *execution accuracy*: the agent's SQL is run, and the answer counts as correct only if it returns the same result as the reference SQL. "Strict" also requires the same column order.

| Test set | Questions | v1 baseline | v4 (final) | v4 strict | Cost of the v4 run |
|---|---|---|---|---|---|
| **Spider dev** (official benchmark) | 1,034 | 78.5% | **81.4%** | 77.8% | $0.24 |
| **Spider-Realistic** (questions reworded so they don't name the columns) | 508 | n/a | **77.6%** | 74.6% | $0.12 |
| **BIRD Mini-Dev** (larger, messier real-world databases) | 500 | n/a | **65.4%** (65.0% with a 30-second query limit) | 65.2% | $0.25 |

**Spider dev by difficulty** (execution accuracy)

| | Easy | Medium | Hard | Extra hard |
|---|---|---|---|---|
| v1 baseline | 88.7% | 81.6% | 78.7% | 54.8% |
| v4 (final) | 91.1% | 83.6% | 82.2% | 60.2% |

What the numbers say:

- **About 4 in 5 answers are exactly right** on typical questions, and accuracy drops on harder and messier data.
- **Rewording costs 3.8 points** (81.4% to 77.6%): part of the score comes from matching column names that appear in the question.
- **BIRD is much harder** (65.4%). Its databases are larger and its questions need more domain knowledge, even with the hints BIRD provides.
- **Cost is low:** the full 1,034-question Spider run cost about 24 cents.

v4 was chosen as the headline version **before** running it on the test sets, and it was frozen before Spider-Realistic and BIRD, so those scores weren't tuned to.

---

## What helped and what didn't

All improvements were developed on a separate set of 100 *training* questions (weighted toward hard ones), so the official test set stayed unseen until the final runs. Running the same version twice changed only about 1 question in 100, so differences of 2 or more are likely real.

| Change | Effect on the 100 training questions | Verdict |
|---|---|---|
| **General rules**: return only the columns asked for, don't reformat values, match values exactly as they appear in the data | **+5** (75 to 80) | Helped |
| **Spider-specific rules**: no `DISTINCT` unless asked, inner joins unless asked | **about +2** (fixed 5, broke 3) | Small gain, and only for this benchmark's conventions, so left out of the headline version |
| **Self-correction**: if the SQL errors or returns nothing, show the model the problem and let it retry (up to 2 times) | **+1** | Within noise: the SQL rarely errored, so there was little to fix |
| **Robust SQL extraction**: ignore any text the model writes before the query | Fixed 1 formatting failure | Small, but cheap insurance |

**Question by question on Spider dev (v1 to v4):**

| | Total | Easy | Medium | Hard | Extra |
|---|---|---|---|---|---|
| Fixed (wrong in v1, right in v4) | **40** | 6 | 17 | 6 | 11 |
| Broke (right in v1, wrong in v4) | **10** | 0 | 8 | 0 | 2 |
| Wrong in both | 182 | 22 | 65 | 31 | 64 |

Net gain: **+30 questions**. Typical fixes: names returned as separate first and last name columns instead of glued together, and an unsupported SQL feature replaced with one that works. Typical breaks: the "only the columns asked for" rule applied too literally (dropping the student column from "How many friends does *each student* have?"), and a filter that matched too broadly.

Full details, including every version's score and cost, are in [`evaluation/results/version_log.md`](evaluation/results/version_log.md).

---

## The app

A Streamlit workspace where you pick one of five sample databases (or upload your own CSV files) and ask questions in plain English.

**Every answer shows its work:**

- **A plain-English answer first**, written for a non-technical reader, then the key number, table, or chart.
- **Number check:** every number in the written answer is verified in code against the actual query result. If one doesn't match, the sentence is thrown away and a simple answer is built directly from the data.
- **Ranking check:** words like "latest", "highest", or "top" are only allowed if the SQL actually sorted or picked that value.
- **Stated assumptions**, such as "used Gross Profit, not Premium or Ad Gross Profit".
- **"Did you mean" options** for ambiguous questions (total vs latest quarter, one profit column vs another). One click reruns the question with that reading.
- **"How I got this answer":** the SQL, the tables used, and trust badges ("Ran successfully", "Needed 1 retry", "Returned no rows", "Answer numbers verified").

**Your own data:** upload up to 3 CSV files (5 MB each). Names are cleaned, types are detected (numbers, text, dates), and messy files are handled with friendly messages: wrong encodings, semicolon separators, duplicate column names, broken rows, empty files, and very wide files. Uploaded data lives only in memory for that session and is never saved to disk.

**Three-layer read-only guard:** the app can never change data.

1. **Text check:** only a single `SELECT` (or `WITH ... SELECT`) query is allowed. Anything containing `DELETE`, `DROP`, `UPDATE`, and similar is refused.
2. **Read-only connection:** databases are opened in read-only mode.
3. **SQLite authorizer:** while a query runs, the database itself refuses every action except reading.

### Two real catches from testing

- **The blocked DELETE.** Asked to "Delete all rows from the sales table", the model wrote `DELETE FROM sales;`. The guard refused to run it, and all 60 rows were still there.
- **The wrong range.** For "give me the profit" on a 26-quarter Spotify dataset, the answer writer only saw the first 20 rows, so it summarized the range from those (329 to 801). The number check rejected it, and the app showed the correct range over all 26 rows (105 to 801). The root cause was then fixed by giving the writer the full range, computed in code.

---

## How it works

1. **Describe the database.** The agent reads every table's columns, keys, and 3 sample rows.
2. **Write the SQL.** That description and the question go to Claude with a short set of rules.
3. **Clean and check the SQL.** Any text around the query is stripped, and the read-only guard checks it.
4. **Run it, and retry if needed.** If the query fails or returns nothing, the model sees what happened and can fix it (up to 2 retries).
5. **Write the answer.** A second, low-cost call turns the result into a 1 to 2 sentence answer, with assumptions and "Did you mean" options.
6. **Verify the answer.** Code checks every number and every ranking word against the result before anything is shown.

### Project layout

```
agent/        SQL generation (sql_agent.py), schema description, answer writing and checks
app/          Streamlit app, CSV uploads, read-only safety guard
app_data/     The five sample databases used by the app
evaluation/   Evaluation script, Spider difficulty labels, results and version log
```

---

## Run it locally

You need Python 3.11 and an [Anthropic API key](https://console.anthropic.com/).

```bash
git clone https://github.com/Riri0101/ask-a-database.git
cd ask-a-database
pip install -r requirements.txt
```

Create a file named `.env` in the project folder containing your key:

```
ANTHROPIC_API_KEY=your_key_here
```

Start the app:

```bash
streamlit run app/streamlit_app.py
```

Then open http://localhost:8501. The `.env` file is listed in `.gitignore`, so your key is never committed. When deploying to Streamlit Community Cloud, add `ANTHROPIC_API_KEY` in the app's Secrets instead.

**To re-run the evaluation**, download the [Spider dataset](https://yale-lily.github.io/spider) into `data/spider/` (and, for BIRD, the [Mini-Dev databases](https://github.com/bird-bench/mini_dev) into `data/MINIDEV/`), then run for example:

```bash
python evaluation/evaluate.py --split dev --limit all --agent-version v4
```

---

## Limitations

- **Benchmarks are not your data.** Spider and BIRD are public datasets. Accuracy on a real company database, with its own naming habits and messy values, is not measured, and the app says so on every uploaded-data answer.
- **The scoring has its own errors.** Some Spider reference answers are wrong (for example, filtering on lowercase text that doesn't exist), some questions have ties where several answers are valid, and a match on an empty result can be luck. The true accuracy is probably a few points higher or lower than reported.
- **Some gains are benchmark-specific.** The Spider-convention rules help the score but may not be what a real user wants, which is why they are kept out of the headline version.
- **The checks catch some mistakes, not all.** The number check confirms that a number appears in the result, not that it's attached to the right row or label. The ranking check works from a list of words. The SQL itself can still answer a slightly different question than the one asked.
- **Answers vary between runs.** The model doesn't phrase things the same way every time, and an ambiguous question can be read differently on different runs.
- **The app's exact setup wasn't scored on the test set.** The app adds self-correction to v4. On the training questions that changed the score by only +1, but that combination wasn't scored on Spider dev.
- **Small demo safeguards.** The daily question limit is stored in a small file and resets if the hosted app restarts. CSV dates like 03/04/2024 are read month-first.
- **One model.** All results are for Claude Haiku 5.5 at medium effort. Other models or effort levels would give different numbers and costs.
