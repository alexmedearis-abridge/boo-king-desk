"""Shared eval plumbing: paths, cases, fixtures, and running one case against the real agent."""
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "bookingdesk"
EVALS = ROOT / "evals"
sys.path.insert(0, str(APP))

import agent  # noqa: E402
import data  # noqa: E402
import llm  # noqa: E402

CASES_DIR = EVALS / "cases"
FIXTURES_DIR = ROOT / "data" / "fixtures"
REPORTS_DIR = EVALS / "reports"
CASE_ID = re.compile(r"^[a-z0-9_]{1,120}$")


def load_fixtures():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES_DIR.glob("*.json"))]


def load_cases():
    return {c["id"]: c for c in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json")))}


def load_partitions():
    return json.loads((EVALS / "partitions.json").read_text())


def new_desk(trace_dir):
    """A fresh desk, seeded identically, plus the synthetic fixtures. data.NOW is already a frozen clock."""
    agent.TRACE_DIR = str(trace_dir)
    desk = agent.Desk()
    for fx in load_fixtures():
        desk.db["patients"].append(copy.deepcopy(fx["patient"]))
        desk.db["referrals"][fx["patient"]["id"]] = copy.deepcopy(fx["referral"])
        desk.db["requests"].append(copy.deepcopy(fx["request"]))
    return desk


def snapshot(desk):
    db = desk.db
    return {
        "slots": [{k: s[k] for k in ("id", "start", "provider", "location", "visit_type", "status", "booked_for")} for s in db["slots"]],
        "requests": copy.deepcopy(db["requests"]),
        "outbox": copy.deepcopy(db["outbox"]),
    }


def patient_record(desk, pid):
    """Everything a grader may need about one patient, from the seeded chart."""
    p = next((x for x in desk.db["patients"] if x["id"] == pid), None)
    return {"patient": p, "referral": desk.db["referrals"].get(pid)}


def _hook(desk, sid, hook):
    if hook["action"] == "flip_offered_slot":
        last = next((t for t in reversed(desk.trace) if t["session"] == sid and t.get("tool") == "search_slots"
                     and t["result"].get("slots")), None)
        if last:
            slots = last["result"]["slots"]
            slot_id = slots[min(hook.get("index", 0), len(slots) - 1)]["slot_id"]
            if desk._slot(slot_id)["status"] == "Available":
                desk.flip_slot(slot_id)
            return {"hook": hook, "slot_id": slot_id}
    return {"hook": hook, "skipped": True}


def run_case(case, run_idx, trace_dir):
    """Play the caller's turns and collect what the graders read. Never raises."""
    desk = new_desk(trace_dir)
    sid = f"eval-{case['id']}-r{run_idx}"[:150]
    # The agent's trace redacts DOB and phone in tool args. Graders need the real values (e.g. the DOB the
    # agent normalized), so record raw args in memory, in call order. They never reach disk (see public_artifact).
    raw_calls = []
    real_run_tool = desk.run_tool

    def recording_run_tool(s_id, name, args):
        raw_calls.append((s_id, name, copy.deepcopy(args)))
        return real_run_tool(s_id, name, args)
    desk.run_tool = recording_run_tool
    before = snapshot(desk)
    turns, errors, hooks = [], [], []
    for i, text in enumerate(case["turns"], 1):
        start = len(desk.trace)
        t0 = time.time()
        try:
            out = desk.chat(sid, text)
        except Exception as e:  # harness must survive agent crashes; they become evidence
            out = {"error": f"{type(e).__name__}: {e}"}
        latency = int((time.time() - t0) * 1000)
        if out.get("error"):
            errors.append({"turn": i, "error": out["error"]})
        turns.append({"turn": i, "caller": text, "reply": out.get("reply", ""), "error": out.get("error"),
                      "latency_ms": latency, "trace_start": start, "trace_end": len(desk.trace)})
        for h in case.get("hooks", []):
            if h.get("after_turn") == i:
                hs = len(desk.trace)
                hooks.append({**_hook(desk, sid, h), "trace_index": hs})
    trace = [dict(t, i=n) for n, t in enumerate(desk.trace) if t["session"] in (sid, "staff")]
    raw = iter(a for s_id, _, a in raw_calls if s_id == sid)
    for t in trace:  # tool trace lines and run_tool calls happen in the same order
        if t.get("kind") == "tool" and t["session"] == sid:
            t["args"] = {k: v for k, v in next(raw, t["args"]).items() if k != "reason"}
    sess = desk.sessions.get(sid, {})
    pid = case.get("patient_id")
    return {
        "case_id": case["id"], "run": run_idx, "session": sid,
        "turns": turns, "hooks": hooks, "errors": errors, "trace": trace,
        "verified_patient": sess.get("verified"),
        "store_before": before, "store_after": snapshot(desk),
        "patient": patient_record(desk, pid) if pid else None,
        "all_patients": [{"patient": p, "referral": desk.db["referrals"].get(p["id"])} for p in desk.db["patients"]],
        "backend": llm.backend_name(),
    }


# ---------- PHI-safe copies for reports, judges and the console ----------
def _names(art):
    out = []
    for rec in art.get("all_patients", []):
        n = rec["patient"]["name"][0]
        out += [f"{n['given'][0]} {n['family']}", n["family"], n["given"][0]]
    return sorted({x for x in out if len(x) > 2}, key=len, reverse=True)


def scrub(text, art):
    """Redact DOB, phone and patient names. Synthetic data, but reports cite ids, never names."""
    if not text:
        return text
    t = agent.DOB_RE.sub("[dob]", text)
    t = agent.PHONE_RE.sub("[phone]", t)
    for n in _names(art):
        t = re.sub(re.escape(n), "[name]", t, flags=re.I)
    return t


def public_artifact(art):
    """What gets written to disk and shown in the console: no names, DOBs or phones."""
    def clean(o):
        if isinstance(o, str):
            return scrub(o, art)
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, dict):
            return {k: ("[redacted]" if k in ("birth_date", "birthDate", "phone", "telecom", "patient_name") else clean(v))
                    for k, v in o.items()}
        return o
    keep = {k: art[k] for k in ("turns", "hooks", "errors", "trace", "verified_patient", "backend")}
    keep["store_after"] = {"slots": [s for s in art["store_after"]["slots"] if s["booked_for"]],
                           "requests": art["store_after"]["requests"]}
    out = clean(keep)
    out.update({k: art[k] for k in ("case_id", "run", "session")})  # our ids, never patient names
    return out
