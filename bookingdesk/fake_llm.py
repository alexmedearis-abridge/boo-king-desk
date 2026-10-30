"""Scripted stand-in for the model (LLM_BACKEND=fake).

It drives the real tools through a plausible call so the eval pipeline can run end to
end without a network. It is plumbing only: its behavior says nothing about quality."""
import itertools
import json
import re

_ids = itertools.count(1)
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
YES = re.compile(r"\b(yes|yeah|yep|sure|ok(ay)?|works|sounds good|first|second|third|that one|book it|go ahead)\b", re.I)
RED = re.compile(r"numb|cold|chest pain|can'?t breathe|faint|severe", re.I)
HUMAN = re.compile(r"\b(person|human|someone real|representative)\b", re.I)


def _tc(tool_name, **args):
    args.setdefault("reason", "scripted")
    return {"text": "", "tool_calls": [{"id": f"fake{next(_ids)}", "name": tool_name, "input": args}]}


def _say(text):
    return {"text": text, "tool_calls": []}


def _dob(text):
    t = text.lower()
    m = re.search(r"\b(19|20)\d\d-\d\d-\d\d\b", t)
    if m:
        return m.group(0)
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(st|nd|rd|th)?,?\s+((19|20)\d\d)\b", t)
    if m:
        return f"{m.group(4)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return None


def _name(text):
    m = re.search(r"(?:this is|it's|i'm|name is|^)\s*([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){1,3})", text.strip())
    return m.group(1) if m else ""


def _results(history):
    """(tool name, parsed result) for every tool result so far, oldest first."""
    names = {}
    out = []
    for h in history:
        if h["role"] == "assistant":
            for tc in h.get("tool_calls", []):
                names[tc["id"]] = tc["name"]
        elif h["role"] == "tool":
            for r in h["results"]:
                out.append((names.get(r["id"]), json.loads(r["content"])))
    return out


def call(system, history, tools):
    if not tools:  # judge or propose-rules call
        return _say(json.dumps({"verdict": "pass", "rationale": "fake model"}))
    res = _results(history)
    verified = any(n == "find_patient" and r.get("match") for n, r in res)
    offered = next((r["slots"] for n, r in reversed(res) if n == "search_slots" and r.get("slots")), [])
    users = [h["content"] for h in history if h["role"] == "user"]
    said = " ".join(users).lower()
    last = history[-1]

    if last["role"] == "tool":
        name, r = res[-1]
        if name == "find_patient":
            if r.get("match"):
                return _tc("get_referral")
            return _tc("escalate", category="identity", summary="Could not verify identity twice.",
                       next_action="Call back to verify identity.") if r.get("attempt", 1) >= 2 \
                else _say("I couldn't match those details. Could you repeat your full name and date of birth?")
        if name == "get_referral":
            tod = "afternoon" if "afternoon" in said else "morning" if "morning" in said else "any"
            loc = "Eastside" if "eastside" in said else "Downtown" if "downtown" in said else "any"
            return _tc("search_slots", visit_type="PT Evaluation", time_of_day=tod, location=loc)
        if name == "search_slots":
            if r.get("blocked"):
                return _say(f"I can't book that: {r['blocked']}")
            if not r.get("slots"):
                return _say("I don't see openings that match. Would other times work?")
            opts = "\n".join(f"{i}. {s['when']} with {s['provider']}" for i, s in enumerate(r["slots"][:3], 1))
            return _say("Here are the options:\n" + opts + "\nWhich works?")
        if name == "book_slot":
            if r.get("ok"):
                return _tc("send_message", body=f"Booked: {r['when']} with {r['provider']}.", attach_intake_form=True)
            return _tc("search_slots", visit_type="PT Evaluation")
        if name == "send_message":
            booked = next((r for n, r in reversed(res) if n == "book_slot" and r.get("ok")), None)
            return _say(f"You're booked for {booked['when']} with {booked['provider']}." if booked else "Sent.")
        if name == "escalate":
            return _say("I've passed this to our front-desk team. Someone will follow up.")
        return _say("Okay.")

    text = last["content"]
    if RED.search(text):
        return _tc("escalate", category="clinical", summary="Caller reported red-flag symptoms.",
                   next_action="Nurse callback.", caller_quote=text[:200])
    if HUMAN.search(text):
        return _tc("escalate", category="human_requested", summary="Caller asked for a person.",
                   next_action="Call the patient back.")
    if not verified:
        dob = _dob(text)
        if dob and _name(text):
            return _tc("find_patient", name=_name(text), birth_date=dob)
        return _say("Can I have your full name and date of birth?")
    if offered and YES.search(text):
        idx = 1 if "second" in text.lower() else 2 if "third" in text.lower() else 0
        return _tc("book_slot", slot_id=offered[min(idx, len(offered) - 1)]["slot_id"])
    tod = "afternoon" if "afternoon" in text.lower() else "morning" if "morning" in text.lower() else "any"
    return _tc("search_slots", visit_type="PT Evaluation", time_of_day=tod)
