"""Judge calibration against human labels.

    python -m evals.calibrate --judge handoff_summary [--rerun-judge]

Reads evals/labels/<judge>.jsonl (from evals.label, sampled from the calibration partition), takes
the majority label per item, splits items train/test with a fixed seed, and reports TPR, TNR and
Cohen's kappa on test. "Positive" = the human says pass. Tune the rubric on train items only.
Writes evals/judges/<judge>.calibration.json with today's date."""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date

from . import common
from .graders import judges as J


def kappa(pairs):
    """Cohen's kappa for a list of (a, b) labels."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def load_labels(judge):
    p = common.EVALS / "labels" / f"{judge}.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    items = defaultdict(list)
    for r in rows:
        items[r["item_id"]].append(r)
    return items


def majority(rs):
    c = Counter(r["label"] for r in rs)
    top = c.most_common()
    return None if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]


def metrics(pairs):
    """pairs: (human, judge)."""
    tp = sum(h == "pass" and j == "pass" for h, j in pairs)
    tn = sum(h == "fail" and j == "fail" for h, j in pairs)
    pos = sum(h == "pass" for h, _ in pairs)
    neg = sum(h == "fail" for h, _ in pairs)
    return {"tpr": tp / pos if pos else None, "tnr": tn / neg if neg else None, "kappa": kappa(pairs), "n": len(pairs)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", required=True, choices=sorted(J.JUDGES))
    ap.add_argument("--rerun-judge", action="store_true", help="re-run the judge now instead of using stored verdicts")
    a = ap.parse_args(argv)
    items = load_labels(a.judge)
    if not items:
        print(f"No labels in evals/labels/{a.judge}.jsonl. Label calibration-partition runs with: python -m evals.label --judge {a.judge} --report <stamp>")
        return 1
    rows = []
    for iid, rs in items.items():
        h = majority(rs)
        if h is None:
            continue
        jv = rs[0].get("judge_verdict")
        if a.rerun_judge:
            art = json.loads((common.REPORTS_DIR / rs[0]["report"] / "artifacts" / f"{rs[0]['case_id']}__r{rs[0]['run']}.json").read_text())
            case = common.load_cases().get(rs[0]["case_id"], {"claims": [], "strata": {}})
            res = J.run(a.judge, case, {**art, "all_patients": []})
            jv = res and res["verdict"]
        split = "test" if int(hashlib.sha256(iid.encode()).hexdigest(), 16) % 2 else "train"
        rows.append({"item": iid, "human": h, "judge": jv, "split": split, "config": rs[0].get("config")})
    test = [(r["human"], r["judge"]) for r in rows if r["split"] == "test" and r["judge"] in ("pass", "fail")]
    train = [(r["human"], r["judge"]) for r in rows if r["split"] == "train" and r["judge"] in ("pass", "fail")]
    n_pass = sum(r["human"] == "pass" for r in rows)
    n_fail = sum(r["human"] == "fail" for r in rows)
    configs = len({r["config"] for r in rows})
    mt = metrics(test)
    rec = {"judge": a.judge, "date": date.today().isoformat(), "n_pass": n_pass, "n_fail": n_fail,
           "n_items": len(rows), "agent_configs": configs, "test": mt, "train": metrics(train),
           "tpr": mt["tpr"] or 0, "tnr": mt["tnr"] or 0, "kappa": mt["kappa"],
           "provisional": n_pass < 35 or n_fail < 35}
    (common.EVALS / "judges" / f"{a.judge}.calibration.json").write_text(json.dumps(rec, indent=1))
    status = J.calibration(a.judge)["status"]
    print(json.dumps(rec, indent=1))
    print(f"\nStatus: {status}" + ("" if configs >= 3 else f"  (warning: labels cover {configs} agent config(s); aim for 3+)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
