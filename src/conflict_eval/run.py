"""Run every (model, case, condition, trial) against a local Ollama server.

Writes one JSON line per response. Re-running with the same --out skips
responses already recorded, so an interrupted run can be resumed.

  python3 -m conflict_eval.run --models llama3.2:3b --trials 10 --out results/raw/main.jsonl
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path

from .checks import score
from .conditions import CONDITIONS, applies, build_messages

OLLAMA = "http://localhost:11434"
TEMPERATURE = 0.7
MAX_TOKENS = 400


def post(path, body):
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def model_digests():
    with urllib.request.urlopen(OLLAMA + "/api/tags") as resp:
        return {m["name"]: m["digest"] for m in json.load(resp)["models"]}


def load_cases(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def done_keys(out):
    if not out.exists():
        return set()
    keys = set()
    for line in out.read_text().splitlines():
        r = json.loads(line)
        keys.add((r["model"], r["case_id"], r["condition"], r["trial"]))
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--cases", default="data/cases.jsonl")
    ap.add_argument("--conditions", nargs="+", default=CONDITIONS)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--limit", type=int, help="only the first N cases (for a pilot)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cases = load_cases(args.cases)[: args.limit]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    skip = done_keys(out)
    digests = model_digests()

    # Trial-major order: finish one complete round (every model, case and
    # condition) before starting the next, so stopping early still leaves
    # balanced data. Analyze only completed rounds (analyze --trials N).
    jobs = [
        (m, c, cond, t)
        for t in range(args.trials)
        for m in args.models
        for c in cases
        for cond in args.conditions
        if applies(cond, c)
        if (m, c["id"], cond, t) not in skip
    ]
    print(f"{len(jobs)} responses to collect ({len(skip)} already in {out})")

    with out.open("a") as f:
        for i, (model, case, cond, trial) in enumerate(jobs, 1):
            started = time.time()
            reply = post("/api/chat", {
                "model": model,
                "messages": build_messages(case, cond),
                "stream": False,
                # The trial number is the seed, so trial t is reproducible.
                "options": {"temperature": TEMPERATURE, "seed": trial, "num_predict": MAX_TOKENS},
            })
            text = reply["message"]["content"]
            f.write(json.dumps({
                "model": model,
                "model_digest": digests.get(model),
                "case_id": case["id"],
                "category": case["category"],
                "factors": case["factors"],
                "condition": cond,
                "trial": trial,
                "temperature": TEMPERATURE,
                "response": text,
                "done_reason": reply.get("done_reason"),
                "seconds": round(time.time() - started, 2),
                **score(case, text),
            }) + "\n")
            f.flush()
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  {model}  {case['id']}  {cond}", flush=True)


if __name__ == "__main__":
    main()
