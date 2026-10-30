"""Turn raw responses into the tables in results/summary.md.

Responses are re-scored from data/cases.jsonl every time, so a corrected
check never needs a re-run of the models.

Trials of the same case are not independent, so every interval here is a
cluster bootstrap that resamples *cases*, not individual responses
(see PREREGISTRATION.md, section 5).

  python3 -m conflict_eval.analyze results/raw/main.jsonl
  python3 -m conflict_eval.analyze results/raw/main.jsonl --audit 100
"""

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from .checks import score
from .conditions import CONDITIONS

SEED = 20261030
REPS = 2000
CATEGORIES = ["injection", "control", "system", "format"]


def load_cases(cases_path):
    return {c["id"]: c for c in map(json.loads, Path(cases_path).read_text().splitlines())}


def load(raw, cases_path, trials=None):
    cases = load_cases(cases_path)
    rows = []
    for line in Path(raw).read_text().splitlines():
        r = json.loads(line)
        if trials is not None and r["trial"] >= trials:
            continue
        r.update(score(cases[r["case_id"]], r["response"]))
        rows.append(r)
    return rows


def case_rates(rows, outcome):
    """{(model, condition, case_id): share of trials where outcome is True}."""
    hits, n = defaultdict(int), defaultdict(int)
    for r in rows:
        key = (r["model"], r["condition"], r["case_id"])
        hits[key] += r[outcome]
        n[key] += 1
    return {k: hits[k] / n[k] for k in n}


def mean(xs):
    return sum(xs) / len(xs)


def bootstrap_ci(values, rng):
    """95% percentile interval for the mean of per-case values."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    means = sorted(mean(rng.choices(values, k=len(values))) for _ in range(REPS))
    return means[int(0.025 * REPS)], means[int(0.975 * REPS) - 1]


def pct(x):
    return "—" if x != x else f"{100 * x:.0f}%"


def fmt(values, rng):
    lo, hi = bootstrap_ci(values, rng)
    return f"{pct(mean(values))} [{pct(lo)}–{pct(hi)}]"


def cases_by(rows):
    """{(model, condition, category): sorted case ids present}."""
    out = defaultdict(set)
    for r in rows:
        out[(r["model"], r["condition"], r["category"])].add(r["case_id"])
    return {k: sorted(v) for k, v in out.items()}


def rate_table(rows, outcome, rng, title):
    rates, groups = case_rates(rows, outcome), cases_by(rows)
    models = sorted({r["model"] for r in rows})
    lines = [f"### {title}", "", "| model | category | " + " | ".join(CONDITIONS) + " |",
             "|---|---|" + "---|" * len(CONDITIONS)]
    for m in models:
        for cat in CATEGORIES:
            if outcome == "violated" and cat == "control":
                continue
            cells = []
            for cond in CONDITIONS:
                ids = groups.get((m, cond, cat))
                cells.append(fmt([rates[(m, cond, i)] for i in ids], rng) if ids else "n/a")
            lines.append(f"| {m} | {cat} | " + " | ".join(cells) + " |")
    return lines


def effect_table(rows, rng):
    """Paired change from baseline, same cases, for each guardrail."""
    lines = ["### Change from baseline (paired by case, percentage points)", "",
             "| model | category | condition | Δ violation | Δ task success |",
             "|---|---|---|---|---|"]
    viol, succ, groups = case_rates(rows, "violated"), case_rates(rows, "success"), cases_by(rows)
    for m in sorted({r["model"] for r in rows}):
        for cat in CATEGORIES:
            base = groups.get((m, "baseline", cat))
            if not base:
                continue
            for cond in CONDITIONS[1:]:
                ids = [i for i in groups.get((m, cond, cat), []) if i in base]
                if not ids:
                    continue
                cells = []
                for rates in (viol, succ):
                    diffs = [rates[(m, cond, i)] - rates[(m, "baseline", i)] for i in ids]
                    lo, hi = bootstrap_ci(diffs, rng)
                    cells.append(f"{100 * mean(diffs):+.0f} [{100 * lo:+.0f}, {100 * hi:+.0f}]")
                if cat == "control":
                    cells[0] = "n/a"
                lines.append(f"| {m} | {cat} | {cond} | {cells[0]} | {cells[1]} |")
    return lines


def style_table(rows):
    """Baseline violation rate by attack style (injection style / user pressure)."""
    lines = ["### Baseline violation rate by attack style", "",
             "| model | category | style | violation rate | responses |", "|---|---|---|---|---|"]
    tally = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["condition"] == "baseline" and r["category"] != "control":
            t = tally[(r["model"], r["category"], r["factors"]["style"])]
            t[0] += r["violated"]
            t[1] += 1
    for (m, cat, style), (v, n) in sorted(tally.items()):
        lines.append(f"| {m} | {cat} | {style} | {pct(v / n)} | {n} |")
    return lines


def consistency_table(rows):
    """How often all trials of a (model, case, condition) agree."""
    lines = ["### Consistency across repeated trials", "",
             "A cell is one model × case × condition. *Mixed* means the trials disagreed: "
             "the same prompt sometimes broke the rule and sometimes did not.", "",
             "| model | cells | always kept rule | always broke rule | mixed |", "|---|---|---|---|---|"]
    rates = case_rates([r for r in rows if r["category"] != "control"], "violated")
    for m in sorted({k[0] for k in rates}):
        vals = [v for k, v in rates.items() if k[0] == m]
        never = sum(v == 0 for v in vals)
        always = sum(v == 1 for v in vals)
        lines.append(f"| {m} | {len(vals)} | {pct(never / len(vals))} | {pct(always / len(vals))} "
                     f"| {pct((len(vals) - never - always) / len(vals))} |")
    return lines


def write_audit(rows, n, path, cases):
    """A random sample for hand-labelling, to measure how often the scorer is wrong.

    The automatic scores go in a separate key file so the labeller is blind to them.
    """
    sample = random.Random(SEED).sample(rows, min(n, len(rows)))
    key_path = path.with_name("audit_key.csv")
    with open(path, "w", newline="") as f, open(key_path, "w", newline="") as k:
        w, kw = csv.writer(f), csv.writer(k)
        w.writerow(["n", "case_id", "system_prompt", "user_task", "document", "response",
                    "human_violated", "human_success", "note"])
        kw.writerow(["n", "model", "case_id", "condition", "trial", "auto_violated", "auto_success"])
        for n_, r in enumerate(sample, 1):
            c = cases[r["case_id"]]
            w.writerow([n_, r["case_id"], c["system"], c["task"], c["document"] or "",
                        r["response"], "", "", ""])
            kw.writerow([n_, r["model"], r["case_id"], r["condition"], r["trial"],
                         int(r["violated"]), int(r["success"])])
    print(f"wrote {len(sample)} responses to {path} (blind) and {key_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("--cases", default="data/cases.jsonl")
    ap.add_argument("--out", default="results/summary.md")
    ap.add_argument("--audit", type=int, help="also write an audit sample of N responses")
    ap.add_argument("--trials", type=int, help="use only trials 0..N-1 (completed rounds)")
    args = ap.parse_args()

    rows = load(args.raw, args.cases, args.trials)
    rng = random.Random(SEED)
    models = sorted({r["model"] for r in rows})
    trials = sorted({r["trial"] for r in rows})
    doc = [
        "# Results", "",
        f"Generated by `conflict_eval.analyze` from `{args.raw}`: {len(rows)} responses, "
        f"{len(models)} models, trials {trials[0]}–{trials[-1]}. "
        f"Cells show the mean over cases with a 95% cluster-bootstrap interval "
        f"({REPS} resamples of cases, seed {SEED}).", "",
        "*Violation* = the model broke the rule under test (acted on the injection, broke the "
        "system rule, or broke the format). *Task success* = it did the legitimate task.", "",
    ]
    for part in (rate_table(rows, "violated", rng, "Violation rate"),
                 rate_table(rows, "success", rng, "Task success rate"),
                 effect_table(rows, rng), style_table(rows), consistency_table(rows)):
        doc += part + [""]
    Path(args.out).write_text("\n".join(doc))
    print(f"wrote {args.out}")
    if args.audit:
        write_audit(rows, args.audit, Path(args.out).with_name("audit_sample.csv"), load_cases(args.cases))


if __name__ == "__main__":
    main()
