"""Spider difficulty labels (easy / medium / hard / extra).

A direct port of Evaluator.eval_hardness from the official Spider evaluation.py
(https://github.com/taoyds/spider). It works on the pre-parsed "sql" field that
every example in dev.json already has, so we don't need the Spider SQL parser.
Quirks of the original are kept on purpose so labels match the official ones.
"""

WHERE_OPS = ("not", "between", "=", ">", "<", ">=", "<=", "!=", "in", "like", "is", "exists")
AGG_NONE = 0  # index of "none" in Spider's AGG_OPS


def _has_agg(unit) -> bool:
    return unit[0] != AGG_NONE


def _count_agg(units) -> int:
    return len([unit for unit in units if _has_agg(unit)])


def _nested_queries(sql: dict) -> list:
    nested = []
    for cond_unit in sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]:
        if isinstance(cond_unit[3], dict):
            nested.append(cond_unit[3])
        if isinstance(cond_unit[4], dict):
            nested.append(cond_unit[4])
    for key in ("intersect", "except", "union"):
        if sql[key] is not None:
            nested.append(sql[key])
    return nested


def _count_component1(sql: dict) -> int:
    """WHERE, GROUP BY, ORDER BY, LIMIT, JOINs, ORs, LIKEs."""
    count = 0
    if len(sql["where"]) > 0:
        count += 1
    if len(sql["groupBy"]) > 0:
        count += 1
    if len(sql["orderBy"]) > 0:
        count += 1
    if sql["limit"] is not None:
        count += 1
    if len(sql["from"]["table_units"]) > 0:
        count += len(sql["from"]["table_units"]) - 1

    and_or = sql["from"]["conds"][1::2] + sql["where"][1::2] + sql["having"][1::2]
    count += len([token for token in and_or if token == "or"])

    cond_units = sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]
    count += len([c for c in cond_units if c[1] == WHERE_OPS.index("like")])
    return count


def _count_component2(sql: dict) -> int:
    """Nested queries and INTERSECT / EXCEPT / UNION."""
    return len(_nested_queries(sql))


def _count_others(sql: dict) -> int:
    """Multiple aggregations, select columns, where conditions, group-by columns."""
    count = 0
    agg_count = _count_agg(sql["select"][1])
    agg_count += _count_agg(sql["where"][::2])
    agg_count += _count_agg(sql["groupBy"])
    if len(sql["orderBy"]) > 0:
        agg_count += _count_agg(
            [unit[1] for unit in sql["orderBy"][1] if unit[1]]
            + [unit[2] for unit in sql["orderBy"][1] if unit[2]]
        )
    agg_count += _count_agg(sql["having"])
    if agg_count > 1:
        count += 1
    if len(sql["select"][1]) > 1:
        count += 1
    if len(sql["where"]) > 1:
        count += 1
    if len(sql["groupBy"]) > 1:
        count += 1
    return count


def spider_hardness(sql: dict) -> str:
    """Return 'easy', 'medium', 'hard' or 'extra' for a parsed Spider query."""
    comp1 = _count_component1(sql)
    comp2 = _count_component2(sql)
    others = _count_others(sql)

    if comp1 <= 1 and others == 0 and comp2 == 0:
        return "easy"
    if (others <= 2 and comp1 <= 1 and comp2 == 0) or (comp1 <= 2 and others < 2 and comp2 == 0):
        return "medium"
    if (
        (others > 2 and comp1 <= 2 and comp2 == 0)
        or (2 < comp1 <= 3 and others <= 2 and comp2 == 0)
        or (comp1 <= 1 and others == 0 and comp2 <= 1)
    ):
        return "hard"
    return "extra"
