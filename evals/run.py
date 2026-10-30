"""Run the eval suite.

    python -m evals.run --partition dev --suite all --runs 3 [--baseline evals/baselines/<name>.json] [--model ...]
    LLM_BACKEND=fake python -m evals.run --partition dev --suite all --runs 1     # plumbing only

Each case runs in a fresh, identically seeded desk with the clock frozen (data.NOW), --runs times.
Inconclusive runs are rerun once and never counted as a pass. Writes evals/reports/<timestamp>/."""
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import aggregate, common
from .graders import GRADERS
from .graders import judges as J

CLAIM_JUDGE = {"C6": "no_clinical_advice", "C7": "handoff_summary", "C8": "readback_before_verify"}


def fake():
    return os.environ.get("LLM_BACKEND") == "fake"


def grade(case, art):
    import llm
    claims = list(dict.fromkeys(["C1"] + [c for c in case["claims"] if c in GRADERS]))
    out = []
    for c in claims:
        g = GRADERS[c](case, art, speakable=llm.speakable) if c == "C8" else GRADERS[c](case, art)
        if c == "C1" and case["expect"].get("expected_verified") and art["verified_patient"] not in (None, case["expect"]["expected_verified"]):
            g = {"claim": "C1", "verdict": "fail", "evidence": g["evidence"] + [{"why": f"verified the wrong chart: {art['verified_patient']}"}]}
        out.append(g)
    return out


def judge(case, art):
    if fake():
        return []
    out = []
    for c, name in CLAIM_JUDGE.items():
        if c in case["claims"]:
            r = J.run(name, case, art)
            if r:
                out.append(r)
    return out


def run_one(case, k, trace_dir, art_dir):
    attempts = 0
    while True:
        attempts += 1
        art = common.run_case(case, k, trace_dir)
        grades = grade(case, art)
        if attempts >= 2 or not any(g["verdict"] == "inconclusive" and _rerunnable(case, g) for g in grades):
            break
    js = judge(case, art)
    rel = f"artifacts/{case['id']}__r{k}.json"
    (art_dir / os.path.basename(rel)).write_text(json.dumps(common.public_artifact(art), ensure_ascii=False))
    return {"case_id": case["id"], "run": k, "suite": case["suite"], "partition": case["partition"],
            "strata": case["strata"], "patient_id": case["patient_id"], "grades": grades, "judges": js,
            "latency_ms": [t["latency_ms"] for t in art["turns"]], "errors": art["errors"],
            "attempts": attempts, "artifact": rel}


def _rerunnable(case, g):
    """Structural inconclusives (claim not applicable to this case) are not worth a rerun."""
    structural = ("not a voice case", "no red-flag turn", "not a booking case")
    return not any(s in e.get("why", "") for e in g["evidence"] for s in structural)


def gold_probe(workers):
    """C8 gold set: one model call per spoken DOB; read the birth_date the agent passes to find_patient."""
    import agent
    import llm
    rows = [json.loads(l) for l in open(common.EVALS / "gold" / "dob_normalization.jsonl")]
    desk = common.new_desk(common.REPORTS_DIR / "_gold_traces")
    system = desk.system_prompt()

    def probe(r):
        try:
            out = llm.call(system, [{"role": "user", "content": f"Hi, this is Riley Jordan, my date of birth is {r['spoken']}."}], agent.TOOLS)
            fp = next((tc for tc in out["tool_calls"] if tc["name"] == "find_patient"), None)
            got = fp["input"].get("birth_date") if fp else None
        except Exception as e:
            return {**r, "got": None, "verdict": "inconclusive", "why": str(e)[:200]}
        if r["expected"] is None:
            v = "pass" if not got else "fail"
        else:
            v = "pass" if got == r["expected"] else "fail"
        return {**r, "got": got, "verdict": v}

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(probe, rows))


def write_report(rdir, meta, agg, results, gold):
    (rdir / "report.json").write_text(json.dumps({"meta": meta, "aggregate": agg, "results": results, "gold": gold}, indent=1))
    pct = lambda x: "–" if x is None else f"{x:.0%}"
    ci = lambda c: "–" if c[0] is None else f"{c[0]:.0%}–{c[1]:.0%}"
    L = [f"# Eval report · {meta['started']}", ""]
    if meta["fake"]:
        L += ["> **FAKE MODEL: plumbing only, not a quality result.**", ""]
    L += [f"**Exit decision: {agg['decision'].upper()}**", ""] + [f"- {r}" for r in agg["reasons"]] + [""]
    L += [f"Partition `{meta['partition']}` · suite `{meta['suite']}` · {meta['runs']} run(s) per case · "
          f"{meta['cases']} cases · model `{meta['backend']}` · {meta['duration_s']} s · "
          f"judge calls {meta['judge_calls']} (≈ ${meta['judge_usd']:.2f})", ""]

    def table(stats):
        rows = ["| Id | Claim | Metric | Result | 95% CI | n | Bar | Status | Worst stratum (gap) |",
                "|---|---|---|---|---|---|---|---|---|"]
        for c, s in stats.items():
            w = s["worst_stratum"]
            ws = "–"
            if w:
                gap = "" if not w["gap"] else f" ({-w['gap']:+.0%} vs overall)"
                ws = f"{w['stratum']} {pct(w['rate'])}{gap}"
            st = s["status"]
            if s["judge"] and meta["calibration"].get(s["judge"]) != "calibrated":
                st += " · uncalibrated, not gating"
            rows.append(f"| {c} | {s['text']} | {s['metric']} | {pct(s['rate'])} | {ci(s['ci95'])} | {s['n']} | {pct(s['bar'])} | {st} | {ws} |")
        return rows

    L += ["## Claims · representative suite", ""] + table(agg["claims"]) + [""]
    if agg["challenge"]:
        L += ["## Claims · challenge suite (reported separately)", ""] + table(agg["challenge"]) + [""]
    if agg["c9"]:
        L += ["## C9 · fairness (worst stratum vs bar − 10 pts)", ""]
        L += [f"- {c}: worst `{v['worst']['stratum']}` {pct(v['worst']['rate'])} (n={v['worst']['n']}), floor {pct(v['floor'])} → "
              f"{'ok' if v['ok'] else 'BELOW'}" for c, v in agg["c9"].items()] + [""]
        gaps = [(c, k, g) for c, v in agg["c9"].items() for k, g in v["coverage_gaps"].items()]
        if gaps:
            L += ["**Coverage gaps** (strata where most cases never reached the claim's decision point, so the rate above "
                  "does not cover them; look at C10 and the artifacts for why):", ""]
            L += [f"- {c} · `{k}`: {g['inconclusive']}/{g['cases']} cases inconclusive" for c, k, g in gaps] + [""]
    L += ["## C10 · latency", "", f"- {agg['latency']['turns']} caller turns · p50 {agg['latency']['p50_ms']} ms · p95 {agg['latency']['p95_ms']} ms", ""]
    if gold:
        g = [x for x in gold if x["verdict"] != "inconclusive"]
        k = sum(x["verdict"] == "pass" for x in g)
        lo, hi = aggregate.wilson(k, len(g))
        L += ["## C8 · DOB gold set", "", f"- {k}/{len(g)} pass ({pct(k / len(g) if g else None)}, 95% CI {ci((lo, hi))})"]
        L += [f"  - fail `{x['id']}` ({x['form']}): heard “{x['spoken']}” → `{x['got']}`, expected `{x['expected']}`" for x in gold if x["verdict"] == "fail"][:15]
        L += [""]
    L += ["## Failures", ""]
    nfail = 0
    for r in results:
        for g in r["grades"] + r["judges"]:
            if g["verdict"] != "fail":
                continue
            nfail += 1
            ev = "; ".join(f"trace #{e['i']}: {e['why']}" if "i" in e else e["why"] for e in g.get("evidence", [])) or g.get("rationale", "")
            L.append(f"- **{g['claim']}** · `{r['case_id']}` run {r['run']} · [{r['artifact']}]({r['artifact']}) · {ev}")
    if not nfail:
        L.append("None.")
    L += ["", "## Judges", "", "| Judge | Claim | Calibration |", "|---|---|---|"]
    L += [f"| {n} | {c} | {meta['calibration'].get(n, 'uncalibrated')} |" for c, n in CLAIM_JUDGE.items()]
    (rdir / "report.md").write_text("\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="dev", choices=["dev", "calibration", "acceptance"])
    ap.add_argument("--suite", default="all", choices=["representative", "challenge", "all"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--baseline")
    ap.add_argument("--model")
    ap.add_argument("--level", default="hackathon", choices=["hackathon", "alpha"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cases", help="comma-separated case ids (debugging)")
    ap.add_argument("--skip-gold", action="store_true")
    a = ap.parse_args(argv)
    if a.model:
        os.environ["MODEL"] = a.model
    import llm
    cases = [c for c in common.load_cases().values() if c["partition"] == a.partition
             and (a.suite == "all" or c["suite"] == a.suite)]
    if a.cases:
        want = set(a.cases.split(","))
        cases = [c for c in cases if c["id"] in want]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    rdir = common.REPORTS_DIR / stamp
    (rdir / "artifacts").mkdir(parents=True)
    t0 = time.time()
    jobs = [(c, k) for c in cases for k in range(1, a.runs + 1)]
    print(f"Running {len(cases)} cases × {a.runs} on {llm.backend_name()} …", flush=True)
    with ThreadPoolExecutor(a.workers) as ex:
        results = list(ex.map(lambda ck: run_one(ck[0], ck[1], rdir / "traces", rdir / "artifacts"), jobs))
    gold = [] if (fake() or a.skip_gold or a.suite == "challenge") else gold_probe(a.workers)
    calib = {n: J.calibration(n)["status"] for n in CLAIM_JUDGE.values()}
    if gold:
        for r in gold:
            results.append({"case_id": f"gold_{r['id']}", "run": 1, "suite": "representative", "partition": a.partition,
                            "strata": {"channel": "voice", "form": r["form"]}, "patient_id": None,
                            "grades": [{"claim": "C8:gold", "verdict": r["verdict"],
                                        "evidence": [{"why": f"{r['spoken']!r} -> {r['got']!r}, expected {r['expected']!r}"}]}],
                            "judges": [], "latency_ms": [], "errors": [], "attempts": 1, "artifact": "gold"})
    baseline = None
    if a.baseline:  # a saved report.json, or just its "aggregate" block
        b = json.loads(open(a.baseline).read())
        baseline = b.get("aggregate", b)
    agg = aggregate.aggregate(results, a.level, calib, baseline)
    meta = {"started": stamp, "partition": a.partition, "suite": a.suite, "runs": a.runs, "cases": len(cases),
            "backend": llm.backend_name(), "fake": fake(), "level": a.level,
            "duration_s": round(time.time() - t0, 1), "judge_calls": J.CALLS["n"], "judge_usd": J.CALLS["usd"],
            "calibration": calib}
    write_report(rdir, meta, agg, [r for r in results if r["artifact"] != "gold"], gold)
    if not fake():  # the console shows the latest real run; plumbing runs don't replace it
        (common.REPORTS_DIR / "latest.txt").write_text(stamp)
    print(f"Report: {rdir / 'report.md'}\nExit decision: {agg['decision'].upper()}")
    for r in agg["reasons"]:
        print(" -", r)
    return 0 if agg["decision"] in ("pass", "non-regression") else 1


if __name__ == "__main__":
    raise SystemExit(main())
