"""The scheduling agent: system prompt, tools, and the agent loop.

The model picks the next tool each step. Tools enforce the hard rules
(identity pinning, referral, policy, provider rules, pre-check before write)
so a bad model decision fails safely and shows up in the trace."""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import data
import llm

TRACE_DIR = os.path.join(os.path.dirname(__file__), "traces")
MAX_STEPS = 14

SYSTEM_PROMPT = """You are the scheduling agent for Bayview Physical Therapy, answering a patient's call (shown as chat).
Today is {now} (Pacific). You book, reschedule and confirm appointments. You are not a clinician.

How you work
- You decide each step: which tool to call, whether to retry, whether to ask the caller or act. Read every tool result before choosing the next step.
- Every tool call takes a short `reason`: one line on why you are making it. It goes in the audit trace.

Identity (hard rule)
- Before saying anything from the chart, verify two identifiers: full name AND date of birth, with find_patient.
- If find_patient finds no match, disclose nothing (do not say whether a patient by that name exists), re-ask once. A second failure means escalate.
- Once verified, the patient ID is pinned for the call. Never look up or discuss another patient.

Scheduling procedure (policy)
{policy}

Booking flow
1. Verify identity. 2. get_referral to see which visit types the referral supports. 3. get_policy and get_preferences (provider rules + saved patient preferences).
4. If the caller has saved preferences, ask whether they still hold. Restate the caller's preferences as structured constraints (days, time of day, location, provider) and confirm before searching.
5. search_slots with those constraints. Offer at most three options, and say briefly why they ranked first (e.g. provider prefers mornings for evaluations).
6. When the caller picks one, book_slot. book_slot re-reads the slot first; if it reports a conflict, apologise, search again and re-offer. Never claim a booking that book_slot did not confirm.
7. After a successful booking, send_message with the confirmation (date, time, provider, location address, arrive 15 minutes early) and the intake form, and save_patient_preferences.
8. If the caller changes a preference mid-call, search again with the new constraints. Do not book until they choose.

Reschedule / cancel
- Find the current booking (get_preferences shows the request row). Search for a new slot, then state clearly: "I'll release <old> and book <new>, OK?" Only after an explicit yes, call reschedule with patient_confirmed=true.

Escalate to staff (escalate tool) when
- identity fails twice; anything clinical; the caller asks for a person; an out-of-policy request you cannot resolve by offering what policy allows; the same step fails twice.
After escalating, tell the caller a team member will follow up, and stop scheduling.

Safety
- No clinical advice: never interpret symptoms, suggest treatment, or discuss medication.
- Red-flag symptoms (numbness, coldness or color change in a limb, chest pain, trouble breathing, severe sudden pain, fainting): stop scheduling, tell the caller to call 911 or go to urgent care now, and escalate with their exact words.
- Only share chart content needed for booking (visit type, referring provider). Never read back date of birth or phone number.
- Policy and the referral always win over preferences.

Style: short, warm, phone-call sentences. One question at a time. Times like "Tuesday Nov 3 at 9:30 AM"."""


TOOLS = [
    {"name": "find_patient", "description": "Look up the caller in the EHR (FHIR Patient search) by full name AND date of birth. Returns match or no match. Must succeed before any chart access.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "birth_date": {"type": "string", "description": "YYYY-MM-DD"},
         "reason": {"type": "string"}}, "required": ["name", "birth_date", "reason"]}},
    {"name": "get_referral", "description": "Read the verified patient's latest referral (DocumentReference -> Binary). Returns referral text and the visit types it authorizes.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "get_policy", "description": "Read the clinic's scheduling procedure: visit lengths, lead time, booking window, locations, rules.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "get_preferences", "description": "Read provider rules (hard = filters, soft = ranking) and the verified patient's saved preferences and Patient Requests row (status, current booking, notes).",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "search_slots", "description": "Search Available Times. Hard rules (referral, policy window, provider hard rules) filter; soft rules and patient preferences rank. Returns up to 5 ranked slots with why.",
     "parameters": {"type": "object", "properties": {
         "visit_type": {"type": "string", "enum": ["PT Evaluation", "PT Follow-up"]},
         "location": {"type": "string", "enum": ["Downtown", "Eastside", "any"]},
         "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
         "days_of_week": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"Tue\",\"Thu\"]; empty = any"},
         "earliest_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
         "latest_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
         "provider": {"type": "string", "description": "optional"},
         "reason": {"type": "string"}}, "required": ["visit_type", "reason"]}},
    {"name": "book_slot", "description": "Book a slot for the verified patient. Re-reads the slot status first and fails with a conflict if it is no longer Available. Marks the slot Booked and the request Booked, linked.",
     "parameters": {"type": "object", "properties": {"slot_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["slot_id", "reason"]}},
    {"name": "reschedule", "description": "Move the patient's current booking to a new slot: books the new slot (with pre-check), then releases the old one, and records the change in the request notes. Requires patient_confirmed=true after an explicit yes.",
     "parameters": {"type": "object", "properties": {
         "new_slot_id": {"type": "string"}, "patient_confirmed": {"type": "boolean"},
         "reason": {"type": "string"}}, "required": ["new_slot_id", "patient_confirmed", "reason"]}},
    {"name": "send_message", "description": "Send an SMS to the verified patient's phone on file (mock). Optionally attach the intake form link.",
     "parameters": {"type": "object", "properties": {
         "body": {"type": "string"}, "attach_intake_form": {"type": "boolean"},
         "reason": {"type": "string"}}, "required": ["body", "reason"]}},
    {"name": "save_patient_preferences", "description": "Save the caller's scheduling preferences to their Patient Requests row for next time.",
     "parameters": {"type": "object", "properties": {
         "preferences": {"type": "object", "properties": {
             "time_of_day": {"type": "string"}, "days_of_week": {"type": "array", "items": {"type": "string"}},
             "location": {"type": "string"}, "provider": {"type": "string"}, "notes": {"type": "string"}}},
         "reason": {"type": "string"}}, "required": ["preferences", "reason"]}},
    {"name": "escalate", "description": "Hand off to front-desk staff: sets the request to Waiting with a Next action and a call summary. Use for identity failure, clinical content, requests for a person, unresolved out-of-policy asks, or repeated failures.",
     "parameters": {"type": "object", "properties": {
         "category": {"type": "string", "enum": ["identity", "clinical", "human_requested", "out_of_policy", "repeated_failure", "other"]},
         "summary": {"type": "string"}, "next_action": {"type": "string"},
         "caller_quote": {"type": "string", "description": "exact words, for clinical escalations"},
         "reason": {"type": "string"}}, "required": ["category", "summary", "next_action", "reason"]}},
]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RED_FLAGS = re.compile(r"numb|cold|tingl|chest pain|can'?t breathe|short(ness)? of breath|faint|pass(ed)? out|turn(ed|ing)? (blue|white|pale)|severe", re.I)
DOB_RE = re.compile(
    r"\b(19|20)\d\d-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(st|nd|rd|th)?,? (19|20)\d\d\b",
    re.I)
PHONE_RE = re.compile(r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b")


def redact(obj):
    """Strip DOB and phone from anything going into the trace."""
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k in ("birth_date", "birthDate", "phone", "telecom") else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        # Keep slot/referral ISO dates readable; only redact when it looks like a DOB context.
        s = PHONE_RE.sub("[phone]", obj)
        return s
    return obj


def fmt_slot(s):
    d = datetime.fromisoformat(s["start"])
    return f"{DAYS[d.weekday()]} {d.strftime('%b')} {d.day} at {d.strftime('%I:%M %p').lstrip('0')}"


class Desk:
    def __init__(self):
        self.lock = threading.RLock()
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.reset()

    def reset(self):
        with self.lock:
            self.db = data.seed()
            self.sessions = {}
            self.trace = []
            self.dry_run = False

    # ---------- session + trace ----------
    def session(self, sid):
        with self.lock:
            if sid not in self.sessions:
                self.sessions[sid] = {"id": sid, "history": [], "transcript": [], "verified": None,
                                      "failures": {}, "busy": False, "started": time.time()}
            return self.sessions[sid]

    def log(self, sid, kind, **kw):
        if kind in ("patient", "agent_reply", "agent_thought") and "text" in kw:
            kw["text"] = DOB_RE.sub("[dob]", kw["text"])
        line = {"ts": datetime.now().isoformat(timespec="milliseconds"), "session": sid, "kind": kind, **redact(kw)}
        with self.lock:
            self.trace.append(line)
        with open(os.path.join(TRACE_DIR, f"{sid}.jsonl"), "a") as f:
            f.write(json.dumps(line) + "\n")

    def _fail(self, s, step):
        s["failures"][step] = s["failures"].get(step, 0) + 1
        return s["failures"][step]

    # ---------- helpers ----------
    def _request_for(self, pid):
        return next((r for r in self.db["requests"] if r["patient_id"] == pid), None)

    def _slot(self, sid):
        return next((x for x in self.db["slots"] if x["id"] == sid), None)

    def _violates_provider_hard(self, slot):
        start = datetime.fromisoformat(slot["start"])
        if slot["provider"] == "Dana Kim, PT" and start.weekday() == 4 and start.hour >= 12:
            return "Friday afternoon reserved for post-op"
        if slot["visit_type"] == "PT Evaluation":
            for o in self.db["slots"]:
                if (o is not slot and o["provider"] == slot["provider"] and o["status"] == "Booked"
                        and o["visit_type"] == "PT Evaluation"):
                    if abs((datetime.fromisoformat(o["start"]) - start).total_seconds()) <= 180 * 60:
                        return "back-to-back with another booked evaluation"
        return None

    def _allowed_visit(self, pid, visit_type):
        ref = self.db["referrals"].get(pid)
        if not ref:
            return "No active referral on file."
        if visit_type not in ref["authorized_visit_types"]:
            return f"Referral does not authorize {visit_type}."
        if visit_type == "PT Follow-up" and not ref["completed_evaluation"]:
            return "Policy: a PT Evaluation must be completed before follow-ups can be booked."
        return None

    def _in_window(self, pid, slot):
        start = datetime.fromisoformat(slot["start"])
        ref = self.db["referrals"][pid]
        if start < data.NOW + timedelta(hours=self.db["policy"]["lead_time_hours"]):
            return "inside 24h lead time"
        if start > data.NOW + timedelta(days=self.db["policy"]["max_days_out"]):
            return "more than 30 days out"
        if not (ref["valid_from"] <= start.date().isoformat() <= ref["valid_to"]):
            return "outside referral window"
        return None

    # ---------- tools ----------
    def run_tool(self, sid, name, args):
        s = self.session(sid)
        pid = s["verified"]
        needs_id = name not in ("find_patient", "get_policy", "escalate")
        if needs_id and not pid:
            return {"error": "Identity not verified. Call find_patient with name and date of birth first."}
        with self.lock:
            return getattr(self, "t_" + name)(s, pid, args)

    def t_find_patient(self, s, pid, a):
        name = " ".join(a.get("name", "").lower().split())
        dob = a.get("birth_date", "").strip()
        for p in self.db["patients"]:
            full = f"{p['name'][0]['given'][0]} {p['name'][0]['family']}".lower()
            if full == name and p["birthDate"] == dob:
                if s["verified"] and s["verified"] != p["id"]:
                    return {"error": "A different patient is already verified on this call. Identity is pinned."}
                s["verified"] = p["id"]
                return {"match": True, "patient_id": p["id"], "name": full.title(),
                        "note": "Verified with 2 identifiers. Patient ID pinned for this call."}
        n = self._fail(s, "identity")
        out = {"match": False, "matches": 0, "attempt": n,
               "instruction": "Disclose nothing about any chart. Re-ask once." if n < 2 else
               "Second failed identity attempt: escalate (category identity) now."}
        return out

    def t_get_referral(self, s, pid, a):
        ref = self.db["referrals"].get(pid)
        if not ref:
            return {"referral": None}
        return {k: ref[k] for k in ("documentReference", "date", "type", "text", "authorized_visit_types",
                                    "valid_from", "valid_to", "completed_evaluation")}

    def t_get_policy(self, s, pid, a):
        return self.db["policy"]

    def t_get_preferences(self, s, pid, a):
        req = self._request_for(pid)
        cur = self._slot(req["selected_slot"]) if req and req["selected_slot"] else None
        return {
            "provider_rules": self.db["provider_prefs"],
            "patient_saved_preferences": req["patient_preferences"] if req else None,
            "request": {k: req[k] for k in ("id", "need", "status", "next_action", "notes")} if req else None,
            "current_booking": ({"slot_id": cur["id"], "when": fmt_slot(cur), "provider": cur["provider"],
                                 "location": cur["location"], "visit_type": cur["visit_type"]} if cur else None),
        }

    def t_search_slots(self, s, pid, a):
        vt = a["visit_type"]
        why_not = self._allowed_visit(pid, vt)
        if why_not:
            self._fail(s, "search")
            return {"slots": [], "blocked": why_not,
                    "instruction": "Explain the limit to the caller and offer what the referral and policy allow."}
        loc = a.get("location") or "any"
        tod = a.get("time_of_day") or "any"
        days = [d[:3].title() for d in (a.get("days_of_week") or [])]
        prov = a.get("provider")
        filtered_out = {}
        ranked = []
        for sl in self.db["slots"]:
            if sl["status"] != "Available" or sl["visit_type"] != vt:
                continue
            start = datetime.fromisoformat(sl["start"])
            if loc != "any" and sl["location"] != loc:
                continue
            if tod == "morning" and start.hour >= 12 or tod == "afternoon" and start.hour < 12:
                continue
            if days and DAYS[start.weekday()] not in days:
                continue
            if prov and prov.lower().split(",")[0] not in sl["provider"].lower():
                continue
            if a.get("earliest_date") and start.date().isoformat() < a["earliest_date"]:
                continue
            if a.get("latest_date") and start.date().isoformat() > a["latest_date"]:
                continue
            bad = self._in_window(pid, sl) or self._violates_provider_hard(sl)
            if bad:
                filtered_out[bad] = filtered_out.get(bad, 0) + 1
                continue
            score, why = 0, []
            if sl["provider"] == "Dana Kim, PT" and vt == "PT Evaluation" and start.hour < 12:
                score += 2; why.append("Dana Kim prefers evaluations before noon")
            if sl["provider"] == "Marcus Webb, PT" and vt == "PT Evaluation" and start.weekday() in (1, 3):
                score += 2; why.append("Marcus Webb prefers evaluations Tue/Thu")
            for rule in self.db["provider_prefs"].get(sl["provider"], {}).get("learned", []):
                why.append(f"learned rule considered: {rule}")
            days_out = (start - data.NOW).days
            score += max(0, 3 - days_out // 3); why.append(f"{days_out} days out")
            ranked.append((score, sl, why))
        ranked.sort(key=lambda x: (-x[0], x[1]["start"]))
        if not ranked:
            self._fail(s, "search")
        return {
            "constraints_applied": {"visit_type": vt, "location": loc, "time_of_day": tod, "days": days or "any", "provider": prov or "any"},
            "hard_filtered_out": filtered_out,
            "slots": [{"slot_id": sl["id"], "when": fmt_slot(sl), "provider": sl["provider"], "location": sl["location"],
                       "visit_type": sl["visit_type"], "minutes": sl["minutes"], "score": sc, "why_ranked": why}
                      for sc, sl, why in ranked[:5]],
        }

    def _book(self, s, pid, slot_id):
        sl = self._slot(slot_id)
        if not sl:
            return None, {"ok": False, "error": f"No slot {slot_id}."}
        # Pre-check: re-read the live record right before writing.
        if sl["status"] != "Available":
            n = self._fail(s, "book")
            return None, {"ok": False, "conflict": True, "slot_id": slot_id, "live_status": sl["status"],
                          "attempt": n, "instruction": "Slot was taken. Apologise, search again, re-offer." if n < 2
                          else "Second booking failure: escalate (repeated_failure)."}
        why_not = self._allowed_visit(pid, sl["visit_type"]) or self._in_window(pid, sl) or self._violates_provider_hard(sl)
        if why_not:
            return None, {"ok": False, "error": f"Not bookable: {why_not}"}
        if self.dry_run:
            return sl, {"ok": True, "dry_run": True, "note": "Dry run: nothing written."}
        sl["status"] = "Booked"
        sl["booked_for"] = pid
        return sl, None

    def t_book_slot(self, s, pid, a):
        sl, err = self._book(s, pid, a["slot_id"])
        if err:
            return err
        req = self._request_for(pid)
        if not self.dry_run:
            req["status"] = "Booked"
            req["selected_slot"] = sl["id"]
            req["next_action"] = ""
            req["review"] = "pending"
            req["notes"].append(f"{data.NOW:%Y-%m-%d} Agent booked {sl['id']} ({fmt_slot(sl)}, {sl['provider']}, {sl['location']}).")
        return {"ok": True, "slot_id": sl["id"], "when": fmt_slot(sl), "provider": sl["provider"],
                "location": sl["location"], "address": self.db["policy"]["locations"][sl["location"]],
                "slot_status": "Booked", "request_status": "Booked", "linked": True}

    def t_reschedule(self, s, pid, a):
        if not a.get("patient_confirmed"):
            return {"ok": False, "error": "Need the patient's explicit yes before releasing the old slot."}
        req = self._request_for(pid)
        old = self._slot(req["selected_slot"]) if req["selected_slot"] else None
        if not old:
            return {"ok": False, "error": "No current booking to reschedule. Use book_slot."}
        sl, err = self._book(s, pid, a["new_slot_id"])
        if err:
            return err
        if not self.dry_run:
            old["status"] = "Available"
            old["booked_for"] = None
            req["selected_slot"] = sl["id"]
            req["status"] = "Booked"
            req["review"] = "pending"
            req["notes"].append(f"{data.NOW:%Y-%m-%d} Rescheduled by patient request: released {old['id']} ({fmt_slot(old)}), booked {sl['id']} ({fmt_slot(sl)}).")
        return {"ok": True, "released": old["id"], "released_when": fmt_slot(old), "booked": sl["id"],
                "when": fmt_slot(sl), "provider": sl["provider"], "location": sl["location"],
                "address": self.db["policy"]["locations"][sl["location"]]}

    def t_send_message(self, s, pid, a):
        p = next(p for p in self.db["patients"] if p["id"] == pid)
        body = a["body"] + ("\nIntake form: https://bayviewpt.example/intake/riley" if a.get("attach_intake_form") else "")
        msg = {"to": p["telecom"][0]["value"], "body": body, "at": datetime.now().strftime("%H:%M:%S"), "dry_run": self.dry_run}
        self.db["outbox"].append(msg)
        return {"ok": True, "sent_to": "phone on file", "attached_intake_form": bool(a.get("attach_intake_form"))}

    def t_save_patient_preferences(self, s, pid, a):
        req = self._request_for(pid)
        req["patient_preferences"] = a["preferences"]
        req["preferred_times"] = ", ".join(str(v) for v in a["preferences"].values() if v)
        return {"ok": True, "saved_to": f"Patient Requests {req['id']}"}

    def t_escalate(self, s, pid, a):
        req = self._request_for(pid) if pid else None
        if req is None:
            # Unverified caller: open a new unlinked request so staff can call back.
            req = {"id": f"R{len(self.db['requests']) + 1:03d}", "patient_name": "Unverified caller", "patient_id": None,
                   "need": "Scheduling call", "status": "New", "phone": "", "preferred_times": "",
                   "patient_preferences": None, "selected_slot": None, "next_action": "", "notes": [], "review": None}
            self.db["requests"].append(req)
        req["status"] = "Waiting"
        req["next_action"] = a["next_action"]
        note = f"{data.NOW:%Y-%m-%d} ESCALATED ({a['category']}): {a['summary']}"
        if a.get("caller_quote"):
            note += f' Caller said: "{a["caller_quote"]}"'
        req["notes"].append(note)
        return {"ok": True, "request": req["id"], "status": "Waiting", "next_action": a["next_action"]}

    # ---------- the loop ----------
    def system_prompt(self):
        pol = self.db["policy"]
        lines = [f"- {r}" for r in pol["rules"]]
        lines += [f"- {k}: {v['minutes']} min, requires {v['requires']}." for k, v in pol["visit_types"].items()]
        lines += [f"- Locations: " + "; ".join(f"{k} ({v})" for k, v in pol["locations"].items())]
        learned = [f"- {p}: {r}" for p, v in self.db["provider_prefs"].items() for r in v.get("learned", [])]
        if learned:
            lines += ["Approved learned rules (from staff corrections):"] + learned
        return SYSTEM_PROMPT.format(now=data.NOW.strftime("%A %b %d %Y, %I:%M %p"), policy="\n".join(lines))

    def chat(self, sid, text):
        s = self.session(sid)
        if s["busy"]:
            return {"error": "Agent is still working on the last message."}
        s["busy"] = True
        try:
            s["transcript"].append({"who": "patient", "text": text})
            self.log(sid, "patient", text=text)
            content = text
            if RED_FLAGS.search(text):
                # Deterministic tripwire alongside the prompt rule; the model still decides the handling.
                self.log(sid, "guardrail", rule="red_flag_language", detail="Red-flag symptom words detected; injected safety note.")
                content += "\n\n[Guardrail: possible red-flag symptom in the caller's words. Follow the Safety rules.]"
            s["history"].append({"role": "user", "content": content})
            reply = ""
            for step in range(MAX_STEPS):
                t0 = time.time()
                out = llm.call(self.system_prompt(), s["history"], TOOLS)
                llm_ms = int((time.time() - t0) * 1000)
                s["history"].append({"role": "assistant", "text": out["text"], "tool_calls": out["tool_calls"]})
                if out["text"]:
                    self.log(sid, "agent_thought" if out["tool_calls"] else "agent_reply", text=out["text"], latency_ms=llm_ms)
                if not out["tool_calls"]:
                    reply = out["text"]
                    break
                results = []
                for tc in out["tool_calls"]:
                    t1 = time.time()
                    try:
                        res = self.run_tool(sid, tc["name"], tc["input"])
                    except Exception as e:  # tool bugs show in the trace instead of killing the call
                        res = {"error": f"{type(e).__name__}: {e}"}
                    args = {k: v for k, v in tc["input"].items() if k != "reason"}
                    self.log(sid, "tool", step=step, tool=tc["name"], reason=tc["input"].get("reason", ""),
                             args=args, result=res, latency_ms=int((time.time() - t1) * 1000), llm_ms=llm_ms,
                             dry_run=self.dry_run)
                    results.append({"id": tc["id"], "content": json.dumps(res)})
                s["history"].append({"role": "tool", "results": results})
            else:
                reply = "Let me get a team member to help you with this."
                self.log(sid, "guardrail", rule="max_steps", detail=f"Stopped after {MAX_STEPS} steps.")
            s["transcript"].append({"who": "agent", "text": reply})
            return {"reply": reply}
        except Exception as e:
            self.log(sid, "error", detail=str(e))
            return {"error": str(e)}
        finally:
            s["busy"] = False

    # ---------- staff side ----------
    def flip_slot(self, slot_id):
        with self.lock:
            sl = self._slot(slot_id)
            sl["status"] = "Available" if sl["status"] == "Booked" else "Booked"
            self.log("staff", "staff", action="flip_slot", slot_id=slot_id, new_status=sl["status"])
            return sl

    def review(self, req_id, verdict, correction=""):
        with self.lock:
            req = next(r for r in self.db["requests"] if r["id"] == req_id)
            req["review"] = verdict
            if verdict == "corrected" and correction:
                sl = self._slot(req["selected_slot"]) if req["selected_slot"] else None
                self.db["corrections"].append({"request": req_id, "booking": fmt_slot(sl) if sl else None,
                                               "provider": sl["provider"] if sl else None, "correction": correction})
            self.log("staff", "staff", action="review", request=req_id, verdict=verdict, correction=correction)

    def propose_rules(self):
        with self.lock:
            corr = list(self.db["corrections"])
        if not corr:
            return {"candidates": []}
        prompt = ("Staff corrected these agent bookings. Turn them into at most 3 short, general scheduling rules for the provider, "
                  "as JSON: [{\"provider\": str, \"rule\": str}]. Reject any rule that selects patients by insurance, language, "
                  "disability or another protected trait. Output JSON only.\n" + json.dumps(corr, indent=1))
        out = llm.call("You turn staff feedback into scheduling rules.", [{"role": "user", "content": prompt}], [])
        m = re.search(r"\[.*\]", out["text"], re.S)
        cands = json.loads(m.group(0)) if m else []
        with self.lock:
            self.db["candidate_rules"] = cands
        self.log("staff", "staff", action="propose_rules", candidates=cands)
        return {"candidates": cands}

    def approve_rule(self, idx):
        with self.lock:
            c = self.db["candidate_rules"].pop(idx)
            self.db["provider_prefs"].setdefault(c["provider"], {"hard": [], "soft": []}).setdefault("learned", []).append(c["rule"])
            self.log("staff", "staff", action="approve_rule", provider=c["provider"], rule=c["rule"])

    def state(self, since=0):
        with self.lock:
            return {
                "backend": llm.backend_name(),
                "now": data.NOW.isoformat(),
                "dry_run": self.dry_run,
                "slots": [{**sl, "label": fmt_slot(sl)} for sl in self.db["slots"]],
                "requests": self.db["requests"],
                "outbox": self.db["outbox"],
                "provider_prefs": self.db["provider_prefs"],
                "candidate_rules": self.db["candidate_rules"],
                "corrections": self.db["corrections"],
                "trace": self.trace[since:],
                "trace_len": len(self.trace),
                "sessions": {k: {"transcript": v["transcript"], "verified": bool(v["verified"]), "busy": v["busy"]}
                             for k, v in self.sessions.items()},
                "system_prompt": self.system_prompt(),
            }
