# Agent version log

Development scores use **train100**: a fixed set of 100 questions from
`train_spider.json` (seed 42), weighted toward hard questions:
15 easy / 25 medium / 30 hard / 30 extra. The official dev set is kept unseen.

Every run uses `claude-haiku-5-5` at effort `medium`. "Accuracy" is execution
accuracy with column order ignored; "strict" also requires the same column order.
Cost is estimated at $0.10 / $0.50 per million input / output tokens.

Run-to-run noise is small, about ±1 question:
- Running v1 twice changed the result of 1 question out of 100. The SQL text differed for 44 questions, but the results were the same.
- v2 and v2b use the same prompt, and their results matched on every question.

So a difference of 2 or more questions is probably real.

| Version | Date | What changed | Accuracy | Strict | easy / medium / hard / extra | Cost (100 q) | Results file |
|---|---|---|---|---|---|---|---|
| v1 | 2026-10-22 | Baseline: schema (columns, keys, 3 sample rows) + question, "return only SQL" | 75/100 | 73/100 | 13/15 · 15/25 · 25/30 · 22/30 | $0.0225 | `claude-haiku-5-5_effort-medium_v1_train100_seed42.csv` |
| v2 | 2026-10-22 | Prompt rules: only the columns asked for, in order; no reformatting (no concatenation, CAST, symbols); exact values from sample rows; no DISTINCT unless asked; inner joins unless asked | 83/100 | 82/100 | 13/15 · 19/25 · 27/30 · 24/30 | $0.0292 | `claude-haiku-5-5_effort-medium_v2_train100_seed42.csv` |
| v3 | 2026-10-22 | v2 + self-correction: if the SQL errors or returns no rows, send question + SQL + problem back, up to 2 fixes | 84/100 | 83/100 | 13/15 · 20/25 · 27/30 · 24/30 | $0.0336 | `claude-haiku-5-5_effort-medium_v3_train100_seed42.csv` |
| v1 (run 2) | 2026-10-22 | Same as v1, run again to measure noise | 76/100 | 75/100 | 13/15 · 15/25 · 26/30 · 22/30 | $0.0221 | `claude-haiku-5-5_effort-medium_v1_train100_seed42_run2.csv` |
| v2a | 2026-10-22 | v1 + general rules only: exact columns asked, in order; no reformatting; match values from sample rows | 80/100 | 79/100 | 13/15 · 18/25 · 26/30 · 23/30 | $0.0269 | `claude-haiku-5-5_effort-medium_v2a_train100_seed42.csv` |
| v2b | 2026-10-22 | v2a + Spider-convention rules: no DISTINCT unless asked; inner joins unless asked (same prompt as v2) | 83/100 | 82/100 | 13/15 · 19/25 · 27/30 · 24/30 | $0.0291 | `claude-haiku-5-5_effort-medium_v2b_train100_seed42.csv` |
| v3a | 2026-10-22 | v2a + self-correction (up to 2 fixes) | 81/100 | 80/100 | 13/15 · 18/25 · 26/30 · 24/30 | $0.0291 | `claude-haiku-5-5_effort-medium_v3a_train100_seed42.csv` |
| **v4** | 2026-10-22 | v2a + robust SQL extraction (`extract_sql`: ignore any text before the first SELECT / WITH … AS). **Official headline version.** | 81/100 | 80/100 | 13/15 · 18/25 · 26/30 · 24/30 | $0.0274 | `claude-haiku-5-5_effort-medium_v4_train100_seed42.csv` |
| v4b | 2026-10-22 | v4 + Spider-convention rules (secondary "benchmark conventions" version) | 84/100 | 83/100 | 14/15 · 19/25 · 27/30 · 24/30 | $0.0294 | `claude-haiku-5-5_effort-medium_v4b_train100_seed42.csv` |

## Official dev scores (all 1,034 dev questions)

| Version | Date | Accuracy | Strict | easy / medium / hard / extra | Cost | Results file |
|---|---|---|---|---|---|---|
| v1 | 2026-10-22 | 812/1034 = 78.5% | 764/1034 = 73.9% | 220/248 · 364/446 · 137/174 · 91/166 | $0.181 | `claude-haiku-5-5_effort-medium_v1_dev_seed42.csv` |
| **v4 (headline)** | 2026-10-22 | **842/1034 = 81.4%** | 804/1034 = 77.8% | 226/248 · 373/446 · 143/174 · 100/166 | $0.239 | `claude-haiku-5-5_effort-medium_v4_dev_seed42.csv` |
| v4b (Spider conventions) | 2026-10-22 | 855/1034 = 82.7% | 818/1034 = 79.1% | 224/248 · 384/446 · 142/174 · 105/166 | $0.257 | `claude-haiku-5-5_effort-medium_v4b_dev_seed42.csv` |

v4 vs v1 on dev: +30 questions (+2.9 points), with the biggest gain on extra (54.8% → 60.2%).
v4b adds +13 more (+1.3 points), mostly on medium.

### v1 → v4 on Spider dev, question by question

Compared from the saved CSVs (no new API calls), using the execution-accuracy result.

| | Count | easy | medium | hard | extra |
|---|---|---|---|---|---|
| Fixed (wrong in v1, right in v4) | **40** | 6 | 17 | 6 | 11 |
| Broken (right in v1, wrong in v4) | **10** | 0 | 8 | 0 | 2 |
| Wrong in both | 182 | 22 | 65 | 31 | 64 |
| Right in both | 802 | | | | |

Net: +30 questions (40 − 10), matching the score change from 812 to 842.

**Fixed, example 1 (#456, medium).** "What are the full names of all players, sorted by birth date?"
- v1 glued first and last name into one column ("Thelma Long").
- v4 returned them as two columns, as Spider expects. The no-reformatting rule did this.

**Fixed, example 2 (#775, extra).** "Which African countries have a smaller population than that of any country in Asia?"
- v1 wrote `< ANY (…)`, which SQLite doesn't support, so the query failed.
- v4 compared against the smallest Asian population and returned the right 5 countries.

**Broken, example 1 (#883, medium).** "How many friends does each student have?"
- v1 returned each student's id with their count.
- v4 returned only the counts, which aren't useful without knowing which student each one belongs to.
- The "only the columns asked for" rule was applied too literally: "each student" implies the student column.

**Broken, example 2 (#731, medium).** "Give the mean GNP and total population of nations which are considered US territory."
- v1 filtered on the exact value 'US Territory'.
- v4 used `LIKE '%US%'`, which also matched other government forms. It reported a total population of 7.4 million instead of 329,000.

**What the 10 broken questions have in common.**
- 2 dropped a "for each …" grouping column (#71, #883).
- 2 picked the wrong filter value (#730, #731).
- 1 used a LEFT JOIN that added zero-count rows (#309). v4 doesn't include the inner-join rule.
- 1 re-sorted dates in a complicated way (#591).
- 4 misread the question or the join, e.g. averaging student ages instead of pet ages (#72).

Some of these flips are probably run-to-run noise: on train100, running the same version twice changed about 1 question in 100, so roughly 10 random changes are expected across 1,034 questions. The two dropped grouping columns, though, are a consistent side effect of the column rule.

## Other test sets (v4, frozen; run 2026-10-22)

v4 was frozen before these runs. Nothing was changed based on them.

| Test set | Questions | Accuracy | Strict | By difficulty | Cost | Results file |
|---|---|---|---|---|---|---|
| Spider-Realistic | 508 | **394/508 = 77.6%** | 379/508 = 74.6% | easy 93/109 · medium 167/203 · hard 72/99 · extra 62/97 | $0.121 | `claude-haiku-5-5_effort-medium_v4_realistic_seed42.csv` |
| BIRD Mini-Dev (SQLite, with hints), 300 s query limit | 500 | **327/500 = 65.4%** | 326/500 = 65.2% | simple 113/148 · moderate 154/250 · challenging 60/102 | $0.254 | `claude-haiku-5-5_effort-medium_v4_bird_seed42.csv` |
| BIRD Mini-Dev, scored with a 30 s query limit | 500 | 325/500 = 65.0% | n/a | simple 113/148 · moderate 153/250 · challenging 59/102 | (same run) | (same file) |

**Spider-Realistic.** These are Spider dev questions reworded so they don't name the columns directly. v4 scores 3.8 points lower here than on Spider dev (77.6% vs 81.4%), so it depends somewhat on column names appearing in the question.

**BIRD.** Questions come from Hugging Face `birdsql/bird_mini_dev` (`mini_dev_sqlite` split); databases come from `data/MINIDEV/dev_databases`.
- Each question's evidence hint was in the prompt (498 of 500 have one).
- Difficulty uses BIRD's own labels.
- Row order counts only when the gold SQL has a top-level ORDER BY, which was detected from the SQL text.

**BIRD, 300 s vs 30 s query limit: −2 questions (−0.4 points).** BIRD's databases are much larger than Spider's, so this run let each query run for up to 300 seconds instead of 30. That limit applied equally to the gold SQL and the agent's SQL.

Two gold queries took longer than 30 seconds:

| Question | Difficulty | Database | Gold query time | Agent's query time | Correct? |
|---|---|---|---|---|---|
| #340 | challenging | codebase_community | 213 s | 0.13 s | Yes |
| #393 | moderate | card_games | 47 s | 0.06 s | Yes |

Under a 30 s limit, both count as wrong, because the gold query couldn't finish, not because the agent erred. None of the agent's own queries took longer than 30 seconds. BIRD's official evaluation script uses a 30 s limit, so 65.0% is the number to compare with published BIRD results. 65.4% reflects what the agent actually got right.

**Timing caveat.** This run shared the machine with three other evaluation runs, so query times may be slightly longer than they would be alone. Every question's times are saved in the `gold_seconds` / `pred_seconds` columns.

One agent query came close to the 30 s limit: #245 (challenging, formula_1) took 29.97 s, and #6 took 16.5 s. Both answers were wrong anyway, so they don't change either score.

## Notes

**v1 mistakes (25 wrong).**
- 7 added `DISTINCT` where Spider keeps duplicates.
- 7 had wrong or extra columns, or reformatted values (e.g. concatenated full names, `SELECT *`).
- 4 were ties under `LIMIT`, where both answers are valid.
- 2 used `LEFT JOIN` and included rows with zero matches.
- 1 had the wrong value casing (`'English'` vs `'english'`).
- 4 were misread or ambiguous questions, or wrong gold SQL (e.g. "highest latitude" where the gold SQL sorts by elevation).

**v2: +8 net (11 fixed, 3 broken).** I first called two of the three breaks noise, but
the v2a run shows that was wrong. All three breaks (#5482, #1947 "Show all directors", #327)
are correct under v2a, so the Spider-convention rules caused them.

**v2a vs v2b: the Spider rules are worth +2 net.**
- They fix 5 questions where DISTINCT or LEFT JOIN changed the result.
- They break the 3 questions above.
- The raw gap is +3, but the extra question is #5428, which v2a missed because of a formatting glitch (see below), not because of these rules.

The general rules alone give +5 over v1 (75 → 80). In a real app, removing duplicates or
including zero-count rows might be what a user wants, so v2a may be the better choice
outside Spider.

**v3: +1, within noise.** Self-correction triggered on 5 of 100 questions:
- 4 had correctly empty answers, which the model kept unchanged.
- 1 (#327) was tried twice and was still wrong.

The one newly correct question (#6005) did not use self-correction. v2's SQL never errored on this set, so there was little to fix. Cost was about 15% higher than v2.

**v3a vs v2a: +1, within noise.** Self-correction again triggered only on 4 correctly
empty answers, and the model kept them unchanged. The one difference, #5428, was a formatting glitch:
- In the v2a run, the model wrote a sentence of reasoning before the SQL. `clean_sql` didn't strip it, so the query failed with a syntax error.
- In the v3a run, the model happened not to write the sentence. Self-correction would have caught it either way, since that is exactly the kind of error it repairs.

A sturdier `clean_sql` that starts at the first SELECT/WITH would also prevent it.

**v4 / v4b: no regressions on train100.**
- v4 vs v2a: +1 (#5428, the formatting glitch, now extracted correctly). Nothing became wrong.
- v4b vs v2b: +1 (#65, within noise). Nothing became wrong.

`extract_sql` changes only 1 of 1,334 saved answers from earlier runs, so it only affects replies with text before the query.

v4 was chosen in advance as the official headline version, because it uses only general rules. v4b reports the score with Spider's conventions added.
