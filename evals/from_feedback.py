"""Close the loop: turn a staff "Correct" into a tracked dev case (Known Model Issue pattern).

    python -m evals.from_feedback <request_id>        # e.g. R001

Finds the staff correction for that request in bookingdesk/traces/staff.jsonl, finds the call that
booked it, and writes evals/cases/kmi_<request>_<stamp>.json in the dev partition. The caller's DOB
is redacted in traces, so it is restored from the synthetic chart. The correction text is kept in
expect._todo: a person turns it into concrete expect fields before the case can fail for the right reason."""
import json
import sys
from datetime import datetime

from . import common


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1 or not argv[0].isalnum():
        print(__doc__)
        return 2
    req_id = argv[0].upper()
    import data
    reqs = {r["id"]: r for r in data.REQUESTS}
    for fx in common.load_fixtures():
        reqs[fx["request"]["id"]] = fx["request"]
    if req_id not in reqs:
        print(f"Unknown request {req_id}")
        return 1
    pid = reqs[req_id]["patient_id"]
    traces = common.APP / "traces"
    staff = [json.loads(l) for l in (traces / "staff.jsonl").read_text().splitlines() if l.strip()] if (traces / "staff.jsonl").exists() else []
    corr = [t for t in staff if t.get("action") == "review" and t.get("request") == req_id and t.get("verdict") == "corrected"]
    if not corr:
        print(f"No staff correction for {req_id} in traces/staff.jsonl")
        return 1
    calls = []
    for p in traces.glob("*.jsonl"):
        if p.stem in ("staff",) or p.stem.startswith("eval-"):
            continue
        ls = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        if any(t.get("tool") == "find_patient" and t["result"].get("patient_id") == pid for t in ls) and \
                any(t.get("tool") in ("book_slot", "reschedule") and t["result"].get("ok") for t in ls):
            calls.append((ls[-1]["ts"], p.stem, ls))
    if not calls:
        print(f"No call in traces/ booked {req_id}")
        return 1
    _, sid, ls = sorted(calls)[-1]
    pat = next(p for p in data.PATIENTS + [f["patient"] for f in common.load_fixtures()] if p["id"] == pid)
    y, m, d = (int(x) for x in pat["birthDate"].split("-"))
    from .spoken import MONTHS
    dob = f"{MONTHS[m - 1].title()} {d} {y}"
    turns = [t["text"].replace("[dob]", dob) for t in ls if t["kind"] == "patient"]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    cid = f"kmi_{req_id.lower()}_{stamp.lower()}"
    case = {"id": cid, "seed": "known_model_issue", "claims": ["C1", "C2", "C3", "C4"], "suite": "representative",
            "partition": "dev", "strata": {"channel": "chat", "name_origin": "unknown", "phrasing": "native",
                                           "noise": "clean", "age_style": "adult"},
            "patient_id": pid, "turns": turns, "hooks": [],
            "expect": {"final_request_status": "Booked", "_from_feedback": corr[-1].get("correction", ""),
                       "_source_call": sid,
                       "_todo": "Translate the staff correction into expect fields (constraints, must_not_book_visit_types, ...)."}}
    (common.CASES_DIR / f"{cid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=1))
    parts = common.load_partitions()
    parts["cases"][cid] = "dev"
    (common.EVALS / "partitions.json").write_text(json.dumps(parts, indent=1, sort_keys=True))
    print(f"Wrote evals/cases/{cid}.json (dev). Correction: {corr[-1].get('correction')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
