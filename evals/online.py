"""Online signals from live demo traffic.

    python -m evals.online [--judge-sample 5]

Reads bookingdesk/traces/*.jsonl (one file per call, plus staff.jsonl) and data/feedback.jsonl if
present. Eval runs write their traces under evals/reports/, so they never mix in here."""
import argparse
import json
import random
from collections import Counter
from datetime import datetime

from . import common
from .graders import judges as J

TRACES = common.APP / "traces"


def load():
    calls, staff = {}, []
    for p in sorted(TRACES.glob("*.jsonl")):
        lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        if p.stem == "staff":
            staff = lines
        elif not p.stem.startswith("eval-"):
            calls[p.stem] = lines
    fb = common.ROOT / "data" / "feedback.jsonl"
    feedback = [json.loads(l) for l in fb.read_text().splitlines() if l.strip()] if fb.exists() else []
    return calls, staff, feedback


def signals(calls, staff, feedback):
    n = len(calls) or 1
    guard = Counter(t["rule"] for ls in calls.values() for t in ls if t.get("kind") == "guardrail")
    esc = Counter(t["args"].get("category", "?") for ls in calls.values() for t in ls if t.get("tool") == "escalate")
    books = [t for ls in calls.values() for t in ls if t.get("tool") in ("book_slot", "reschedule")]
    conflicts = sum(1 for t in books if t["result"].get("conflict"))
    reviews = Counter(t.get("verdict") for t in staff if t.get("action") == "review")
    reviews.update(r.get("verdict") for r in feedback)
    decided = reviews["approved"] + reviews["corrected"]
    return {
        "calls": len(calls),
        "guardrail_rate": {k: v / n for k, v in guard.items()},
        "escalation_rate_by_reason": {k: v / n for k, v in esc.items()},
        "staff_approve_rate": reviews["approved"] / decided if decided else None,
        "staff_correct_rate": reviews["corrected"] / decided if decided else None,
        "staff_reviews": decided,
        "slot_conflict_rate": conflicts / len(books) if books else None,
        "booking_attempts": len(books),
        "stt_normalization_rate": None,
        "stt_note": "not instrumented: voice input is the browser's speech-to-text and the agent normalizes dates "
                    "inside find_patient, so no stt_normalize events exist yet",
    }


def sampled_judge(calls, k):
    """C7 online: judge a random sample of real handoffs (traces already have DOB and phone redacted)."""
    items = [(sid, ls) for sid, ls in calls.items() if any(t.get("tool") == "escalate" for t in ls)]
    out = []
    for sid, ls in random.Random(0).sample(items, min(k, len(items))):
        turns = []
        for t in ls:
            if t["kind"] == "patient":
                turns.append({"caller": t["text"], "reply": ""})
            elif t["kind"] == "agent_reply" and turns:
                turns[-1]["reply"] = t["text"]
        art = {"turns": turns, "trace": ls, "all_patients": []}
        r = J.run("handoff_summary", {"claims": ["C7"], "strata": {}}, art)
        if r:
            out.append({"call": sid, **r})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge-sample", type=int, default=0)
    a = ap.parse_args(argv)
    calls, staff, feedback = load()
    s = signals(calls, staff, feedback)
    if a.judge_sample:
        s["c7_sampled_judge"] = sampled_judge(calls, a.judge_sample)
        s["c7_note"] = f"judge {J.calibration('handoff_summary')['status']}: reported, never gating"
    common.REPORTS_DIR.mkdir(exist_ok=True)
    p = common.REPORTS_DIR / f"online-{datetime.now():%Y%m%dT%H%M%S}.json"
    p.write_text(json.dumps(s, indent=1))
    print(json.dumps(s, indent=1))
    print(f"\nWrote {p}")


if __name__ == "__main__":
    main()
