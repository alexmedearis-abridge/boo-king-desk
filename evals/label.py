"""Tiny labeling CLI for judge calibration.

    python -m evals.label --judge handoff_summary --report <stamp> --annotator alex
    python -m evals.label --judge handoff_summary --agreement

Shows one PHI-scrubbed transcript and the judge's question, records pass/fail with an annotator id
to evals/labels/<judge>.jsonl. Use runs from the calibration partition. Two annotators can label the
same item; --agreement reports Cohen's kappa between them."""
import argparse
import json
import re
from collections import defaultdict

from . import common
from .calibrate import kappa
from .graders import judges as J


def question(judge):
    txt = (J.JUDGE_DIR / f"{judge}.md").read_text()
    m = re.search(r"\*\*Question:\*\*\s*(.+)", txt)
    return m.group(1).strip() if m else txt.splitlines()[0]


def agreement(judge):
    p = common.EVALS / "labels" / f"{judge}.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    by = defaultdict(dict)
    for r in rows:
        by[r["item_id"]][r["annotator"]] = r["label"]
    pairs = [tuple(list(v.values())[:2]) for v in by.values() if len(v) >= 2]
    k = kappa(pairs)
    print(f"{len(pairs)} items with two annotators · agreement {sum(a == b for a, b in pairs)}/{len(pairs)} · "
          f"Cohen's kappa {'–' if k is None else f'{k:.2f}'}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", required=True, choices=sorted(J.JUDGES))
    ap.add_argument("--report", help="report stamp under evals/reports/")
    ap.add_argument("--annotator")
    ap.add_argument("--agreement", action="store_true")
    a = ap.parse_args(argv)
    if a.agreement:
        return agreement(a.judge)
    if not (a.report and a.annotator):
        ap.error("--report and --annotator are required to label")
    rdir = common.REPORTS_DIR / a.report
    rep = json.loads((rdir / "report.json").read_text())
    if rep["meta"]["partition"] != "calibration":
        print(f"Warning: report partition is {rep['meta']['partition']!r}; labels should come from 'calibration'.")
    out = common.EVALS / "labels" / f"{a.judge}.jsonl"
    out.parent.mkdir(exist_ok=True)
    done = set()
    if out.exists():
        done = {(r["item_id"], r["annotator"]) for r in map(json.loads, out.read_text().splitlines()) if r}
    claim = J.JUDGES[a.judge]
    cases = common.load_cases()
    q = question(a.judge)
    for r in rep["results"]:
        if claim not in cases.get(r["case_id"], {}).get("claims", []):
            continue
        iid = f"{a.report}:{r['case_id']}:r{r['run']}"
        if (iid, a.annotator) in done:
            continue
        art = json.loads((rdir / r["artifact"]).read_text())
        jv = next((j["verdict"] for j in r.get("judges", []) if j.get("judge") == a.judge), None)
        print("\n" + "=" * 72 + f"\n{iid}\n")
        for t in art["turns"]:
            print(f"CALLER: {t['caller']}\nAGENT:  {t['reply'] or t.get('error')}\n")
        for t in art["trace"]:
            if t.get("tool") == "escalate":
                print("HANDOFF:", json.dumps(t["args"], ensure_ascii=False))
        print(f"\nQ: {q}")
        ans = input("[p]ass / [f]ail / [s]kip / [q]uit: ").strip().lower()[:1]
        if ans == "q":
            break
        if ans not in ("p", "f"):
            continue
        with open(out, "a") as f:
            f.write(json.dumps({"item_id": iid, "report": a.report, "case_id": r["case_id"], "run": r["run"],
                                "annotator": a.annotator, "label": "pass" if ans == "p" else "fail",
                                "judge_verdict": jv, "config": rep["meta"]["backend"]}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
