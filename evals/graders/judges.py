"""LLM judges for the three claims that need judgment: C6 (no clinical advice), C7 (handoff
sufficiency) and C8 (read-back before verify). Pointwise, binary, temperature 0, same gateway
client as the agent. A judge only gates the suite when its calibration record says so."""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from .. import common

JUDGE_DIR = common.EVALS / "judges"
JUDGES = {"no_clinical_advice": "C6", "handoff_summary": "C7", "readback_before_verify": "C8"}
COST = {"claude": 0.05, "gpt": 0.01}
CALLS = {"n": 0, "usd": 0.0}


def judge_model():
    return os.environ.get("JUDGE_MODEL") or os.environ.get("MODEL") or "claude-sonnet-5-5"


def calibration(name):
    """{"status": "calibrated|provisional|uncalibrated", ...} from <judge>.calibration.json."""
    p = JUDGE_DIR / f"{name}.calibration.json"
    if not p.exists():
        return {"status": "uncalibrated", "why": "no calibration record"}
    c = json.loads(p.read_text())
    age = (date.today() - datetime.strptime(c["date"], "%Y-%m-%d").date()).days
    ok = (c.get("n_pass", 0) >= 35 and c.get("n_fail", 0) >= 35 and c.get("tpr", 0) >= 0.85
          and c.get("tnr", 0) >= 0.85 and age <= 30)
    if c.get("provisional"):
        return {**c, "status": "provisional", "why": "provisional labels (hackathon)"}
    return {**c, "status": "calibrated" if ok else "uncalibrated",
            "why": "" if ok else "below thresholds or older than 30 days"}


def transcript(art):
    lines = []
    for t in art["turns"]:
        lines.append(f"CALLER: {common.scrub(t['caller'], art)}")
        lines.append(f"AGENT: {common.scrub(t['reply'] or t.get('error') or '', art)}")
    return "\n".join(lines)


def payload(name, case, art):
    """What the judge sees, PHI-scrubbed. None means the judge has nothing to judge here."""
    if name == "handoff_summary":
        esc = [t for t in art["trace"] if t.get("tool") == "escalate"]
        if not esc:
            return None
        return {"transcript": transcript(art),
                "handoffs": [{k: common.scrub(str(v), art) for k, v in t["args"].items()} for t in esc]}
    if name == "readback_before_verify":
        if case.get("strata", {}).get("channel") != "voice":
            return None
    return {"transcript": transcript(art)}


def run(name, case, art):
    """{"claim": "C6:judge", "verdict", "rationale", "calibration"} or None when not applicable."""
    import llm
    body = payload(name, case, art)
    if body is None:
        return None
    rubric = (JUDGE_DIR / f"{name}.md").read_text()
    model = judge_model()
    CALLS["n"] += 1
    CALLS["usd"] += COST["gpt"] if "gpt" in model.lower() else COST["claude"]
    try:
        out = llm.call(rubric, [{"role": "user", "content": json.dumps(body, ensure_ascii=False)}], [],
                       temperature=0, model=os.environ.get("JUDGE_MODEL"))
        m = re.search(r"\{.*\}", out["text"], re.S)
        res = json.loads(m.group(0)) if m else {}
        v = res.get("verdict") if res.get("verdict") in ("pass", "fail") else "inconclusive"
        why = res.get("rationale", "unparseable judge output")
    except Exception as e:
        v, why = "inconclusive", f"judge error: {type(e).__name__}: {e}"
    return {"claim": f"{JUDGES[name]}:judge", "judge": name, "verdict": v, "rationale": why,
            "calibration": calibration(name)["status"]}
