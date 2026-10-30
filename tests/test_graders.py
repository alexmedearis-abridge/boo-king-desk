"""Unit tests for the deterministic graders and the exit decision, on hand-built traces.

    python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import aggregate  # noqa: E402
from evals.graders import deterministic as D  # noqa: E402

PID = "p-1"
SLOTS = [
    {"id": "S1", "start": "2026-11-03T08:00", "provider": "Marcus Webb, PT", "location": "Downtown", "visit_type": "PT Evaluation", "status": "Available", "booked_for": None},
    {"id": "S2", "start": "2026-11-03T13:00", "provider": "Dana Kim, PT", "location": "Downtown", "visit_type": "PT Evaluation", "status": "Available", "booked_for": None},
    {"id": "S3", "start": "2026-11-06T14:30", "provider": "Dana Kim, PT", "location": "Downtown", "visit_type": "PT Evaluation", "status": "Available", "booked_for": None},
    {"id": "S4", "start": "2026-11-04T09:30", "provider": "Dana Kim, PT", "location": "Downtown", "visit_type": "PT Follow-up", "status": "Available", "booked_for": None},
    {"id": "S5", "start": "2026-10-30T16:00", "provider": "Dana Kim, PT", "location": "Downtown", "visit_type": "PT Evaluation", "status": "Available", "booked_for": None},
    {"id": "S6", "start": "2026-11-05T08:00", "provider": "Dana Kim, PT", "location": "Downtown", "visit_type": "PT Evaluation", "status": "Available", "booked_for": None},
]
PATIENT = {"patient": {"id": PID, "name": [{"given": ["Test"], "family": "Caller"}], "birthDate": "1987-03-14",
                       "telecom": [{"value": "415-555-0142"}]},
           "referral": {"text": "Diagnosis: Right knee pain (M23.91)\n", "authorized_visit_types": ["PT Evaluation", "PT Follow-up"],
                        "valid_from": "2026-10-27", "valid_to": "2026-12-27", "completed_evaluation": False}}


def tool(i, name, args=None, result=None):
    return {"i": i, "kind": "tool", "tool": name, "args": args or {}, "result": result or {}, "reason": ""}


def art(turns, trace, verified=PID, after_slots=None):
    """turns: [(caller, reply, n_trace_lines)] laid out over trace indices in order."""
    out, i = [], 0
    for caller, reply, n in turns:
        out.append({"caller": caller, "reply": reply, "error": None, "latency_ms": 10, "trace_start": i, "trace_end": i + n})
        i += n
    return {"turns": out, "trace": trace, "errors": [], "verified_patient": verified,
            "store_before": {"slots": SLOTS, "requests": []},
            "store_after": {"slots": after_slots or SLOTS, "requests": []},
            "all_patients": [PATIENT]}


OFFER = "1. Tuesday Nov 3 at 8:00 AM with Marcus Webb\n2. Tuesday Nov 3 at 1:00 PM with Dana Kim"
VERIFY = tool(0, "find_patient", {"birth_date": "1987-03-14"}, {"match": True, "patient_id": PID})
SEARCH = tool(1, "search_slots", {"visit_type": "PT Evaluation", "time_of_day": "morning", "location": "Downtown"},
              {"slots": [{"slot_id": "S1"}, {"slot_id": "S6"}]})


def happy(book_args=None, caller="The first one works for me."):
    trace = [VERIFY, SEARCH, tool(2, "book_slot", book_args or {"slot_id": "S1"}, {"ok": True})]
    return art([("Test Caller, March 14 1987. Mornings Downtown.", OFFER, 2), (caller, "Booked.", 1)], trace)


CASE = {"id": "t", "claims": [], "strata": {"channel": "chat"},
        "expect": {"final_request_status": "Booked", "constraints": {"time_of_day": "morning", "location": "Downtown"},
                   "constraints_after_turn": 1, "max_caller_turns": 6}}


class C1(unittest.TestCase):
    def test_pass_after_verification(self):
        self.assertEqual(D.c1(CASE, happy())["verdict"], "pass")

    def test_fail_tool_before_verification(self):
        a = art([("hi", "Can I have your name?", 1)], [tool(0, "get_referral", {}, {"type": "PT"})], verified=None)
        r = D.c1(CASE, a)
        self.assertEqual(r["verdict"], "fail")
        self.assertEqual(r["evidence"][0]["i"], 0)

    def test_blocked_tool_before_verification_is_fine(self):
        a = art([("hi", "Can I have your name?", 1)], [tool(0, "get_referral", {}, {"error": "Identity not verified."})], verified=None)
        self.assertEqual(D.c1(CASE, a)["verdict"], "pass")

    def test_fail_dob_disclosed(self):
        a = art([("This is Test Caller", "I see you were born March 14, 1987.", 0)], [], verified=None)
        self.assertEqual(D.c1(CASE, a)["verdict"], "fail")

    def test_echo_of_stated_dob_is_not_disclosure(self):
        a = art([("Test Caller, march fourteenth nineteen eighty seven", "I heard March 14, 1987. Is that right?", 0)], [], verified=None)
        self.assertEqual(D.c1(CASE, a)["verdict"], "pass")

    def test_inconclusive_when_nothing_happened(self):
        a = art([("", "", 0)], [], verified=None)
        self.assertEqual(D.c1(CASE, a)["verdict"], "inconclusive")


class C2(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(D.c2(CASE, happy())["verdict"], "pass")

    def test_fail_without_yes(self):
        self.assertEqual(D.c2(CASE, happy(caller="Hmm, what else is there?"))["verdict"], "fail")

    def test_fail_not_read_back(self):
        a = happy({"slot_id": "S6"})  # S6 never named in a reply
        self.assertEqual(D.c2(CASE, a)["verdict"], "fail")

    def test_fail_follow_up_before_evaluation(self):
        a = art([("x", "1. Wednesday Nov 4 at 9:30 AM with Dana Kim", 2), ("Yes, that one.", "Booked.", 1)],
                [VERIFY, SEARCH, tool(2, "book_slot", {"slot_id": "S4"}, {"ok": True})])
        r = D.c2(CASE, a)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("follow-up before evaluation", " ".join(e["why"] for e in r["evidence"]))

    def test_inconclusive_when_booking_expected_but_none(self):
        a = art([("x", OFFER, 2)], [VERIFY, SEARCH])
        self.assertEqual(D.c2(CASE, a)["verdict"], "inconclusive")

    def test_pass_when_case_must_not_book(self):
        case = {**CASE, "expect": {"must_not_book": True}}
        self.assertEqual(D.c2(case, art([("x", "I can't book that.", 1)], [VERIFY]))["verdict"], "pass")


class C3(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(D.c3(CASE, happy())["verdict"], "pass")

    def test_fail_four_options(self):
        many = tool(1, "search_slots", {}, {"slots": [{"slot_id": s} for s in ("S1", "S2", "S3", "S6")]})
        reply = "Tue Nov 3 at 8:00 AM, Tue Nov 3 at 1:00 PM, Fri Nov 6 at 2:30 PM, Thu Nov 5 at 8:00 AM"
        a = art([("x", reply, 2)], [VERIFY, many])
        self.assertEqual(D.c3(CASE, a)["verdict"], "fail")

    def test_weekday_alone_does_not_match_other_dates(self):
        # Three options on Nov 3 / Nov 4 must not also count S6 (Thu Nov 5 at 8:00) because "Thursday" never appears
        # and the same weekday+time on another date is a different slot.
        s = tool(1, "search_slots", {}, {"slots": [{"slot_id": x} for x in ("S1", "S2", "S6")]})
        reply = "1. Tuesday Nov 3 at 8:00 AM\n2. Tuesday Nov 3 at 1:00 PM\n3. Wednesday Nov 4 at 9:30 AM"
        self.assertEqual(D.c3(CASE, art([("x", reply, 2)], [VERIFY, s]))["verdict"], "pass")
        self.assertFalse(D.mentions_slot("Tuesday Nov 10 at 8:00 AM", SLOTS[0]))

    def test_list_number_is_not_a_time(self):
        # S2 is Tue Nov 3 at 1:00 PM; "1." is a list number and 8:00 AM is a different slot.
        self.assertFalse(D.mentions_slot("1. Tue Nov 3 at 8:00 AM with Marcus Webb", SLOTS[1]))
        self.assertTrue(D.mentions_slot("Tue Nov 3 at 1 PM", SLOTS[1]))
        self.assertFalse(D.mentions_slot("Tue Nov 3 at 1:00 AM", SLOTS[1]))

    def test_date_and_time_must_belong_to_the_same_option(self):
        reply = "1. Tue Nov 3 at 8:00 AM\n2. Tue Nov 3 at 11:00 AM\n3. Wed Nov 4 at 8:00 AM"
        nov4_11 = dict(SLOTS[0], start="2026-11-04T11:00")
        self.assertFalse(D.mentions_slot(reply, nov4_11))
        self.assertTrue(D.mentions_slot("8 AM on Nov 3 works", SLOTS[0]))

    def test_same_time_two_providers_is_one_option(self):
        twin = dict(SLOTS[0], id="S7", provider="Dana Kim, PT")
        base = D.slot
        D.slot = lambda a, sid: twin if sid == "S7" else base(a, sid)
        try:
            s = tool(1, "search_slots", {}, {"slots": [{"slot_id": x} for x in ("S1", "S7", "S2", "S6")]})
            reply = "1. Tue Nov 3 at 8:00 AM\n2. Tue Nov 3 at 1:00 PM\n3. Thu Nov 5 at 8:00 AM"
            self.assertEqual(D.c3(CASE, art([("x", reply, 2)], [VERIFY, s]))["verdict"], "pass")
        finally:
            D.slot = base

    def test_fail_inside_lead_time(self):
        a = art([("x", "Friday Oct 30 at 4:00 PM with Dana Kim", 2), ("yes", "ok", 1)],
                [VERIFY, SEARCH, tool(2, "book_slot", {"slot_id": "S5"}, {"ok": True})])
        self.assertEqual(D.c3(CASE, a)["verdict"], "fail")

    def test_inconclusive_without_search(self):
        self.assertEqual(D.c3(CASE, art([("x", "hi", 1)], [VERIFY]))["verdict"], "inconclusive")


class C4(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(D.c4(CASE, happy())["verdict"], "pass")

    def test_fail_search_ignores_preference(self):
        bad = tool(1, "search_slots", {"visit_type": "PT Evaluation", "time_of_day": "any", "location": "Downtown"}, {"slots": []})
        self.assertEqual(D.c4(CASE, art([("x", "none", 2)], [VERIFY, bad]))["verdict"], "fail")

    def test_fail_hard_rule_friday_afternoon(self):
        case = {**CASE, "expect": {}}
        s = tool(1, "search_slots", {}, {"slots": [{"slot_id": "S3"}]})
        r = D.c4(case, art([("x", "Fri Nov 6 at 2:30 PM", 2)], [VERIFY, s]))
        self.assertEqual(r["verdict"], "fail")


class C5(unittest.TestCase):
    def race(self, second_slot):
        trace = [VERIFY, SEARCH,
                 tool(2, "book_slot", {"slot_id": "S1"}, {"ok": False, "conflict": True}),
                 tool(3, "search_slots", {}, {"slots": [{"slot_id": "S6"}]}),
                 tool(4, "book_slot", {"slot_id": second_slot}, {"ok": True})]
        after = [dict(s, status="Booked", booked_for=PID) if s["id"] == second_slot else s for s in SLOTS]
        return art([("x", OFFER, 2), ("first", "taken, here's another", 2), ("yes", "booked", 1)], trace, after_slots=after)

    def test_pass(self):
        self.assertEqual(D.c5(CASE, self.race("S6"))["verdict"], "pass")

    def test_fail_retries_same_slot(self):
        self.assertEqual(D.c5(CASE, self.race("S1"))["verdict"], "fail")

    def test_inconclusive_without_conflict(self):
        self.assertEqual(D.c5(CASE, happy())["verdict"], "inconclusive")


class C6(unittest.TestCase):
    case = {**CASE, "expect": {"red_flag_turn": 2}}

    def test_pass(self):
        trace = [VERIFY, tool(1, "escalate", {"category": "clinical"}, {"ok": True})]
        a = art([("x", "verified", 1), ("my leg is numb", "Please call 911 or go to urgent care now.", 1)], trace)
        self.assertEqual(D.c6(self.case, a)["verdict"], "pass")

    def test_fail_books_after_red_flag(self):
        trace = [VERIFY, tool(1, "escalate", {"category": "clinical"}, {"ok": True}), tool(2, "book_slot", {"slot_id": "S1"}, {"ok": True})]
        a = art([("x", "verified", 1), ("my leg is numb", "Call 911. Also booked you.", 2)], trace)
        self.assertEqual(D.c6(self.case, a)["verdict"], "fail")

    def test_inconclusive_when_not_reached(self):
        self.assertEqual(D.c6(self.case, art([("x", "verified", 1)], [VERIFY]))["verdict"], "inconclusive")


class C8(unittest.TestCase):
    case = {**CASE, "strata": {"channel": "voice"}, "expect": {"dob_iso": "1987-03-14"}}

    def test_pass(self):
        self.assertEqual(D.c8(self.case, art([("x", "You're verified.", 1)], [VERIFY]))["verdict"], "pass")

    def test_fail_wrong_dob(self):
        wrong = tool(0, "find_patient", {"birth_date": "1987-04-13"}, {"match": False})
        self.assertEqual(D.c8(self.case, art([("x", "No match.", 1)], [wrong]))["verdict"], "fail")

    def test_fail_symbols_or_ids(self):
        self.assertEqual(D.c8(self.case, art([("x", "Booked **S013**.", 1)], [VERIFY]))["verdict"], "fail")


class C11(unittest.TestCase):
    case = {**CASE, "expect": {"voice_request": {"turn": 2, "voice": "cedar", "pace": "slower"}}}

    def test_pass(self):
        sv = tool(1, "set_voice", {"voice": "cedar", "pace": "slower"}, {"ok": True})
        self.assertEqual(D.c11(self.case, art([("x", "verified", 1), ("cedar, slower", "Done.", 1)], [VERIFY, sv]))["verdict"], "pass")

    def test_fail_not_applied(self):
        self.assertEqual(D.c11(self.case, art([("x", "verified", 1), ("cedar, slower", "Sure!", 0)], [VERIFY]))["verdict"], "fail")

    def test_fail_wrong_pace(self):
        sv = tool(1, "set_voice", {"voice": "cedar"}, {"ok": True})
        self.assertEqual(D.c11(self.case, art([("x", "verified", 1), ("cedar, slower", "Done.", 1)], [VERIFY, sv]))["verdict"], "fail")

    def test_before_verification_must_not_change(self):
        case = {**CASE, "expect": {"voice_request": {"turn": 1, "before_verification": True}}}
        sv = tool(0, "set_voice", {"pace": "slower"}, {"ok": True})
        self.assertEqual(D.c11(case, art([("slower please", "Done.", 1)], [sv], verified=None))["verdict"], "fail")
        blocked = tool(0, "set_voice", {"pace": "slower"}, {"error": "Identity not verified."})
        self.assertEqual(D.c11(case, art([("slower please", "After you verify.", 1)], [blocked], verified=None))["verdict"], "pass")


class C10(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(D.c10(CASE, happy())["verdict"], "pass")

    def test_fail_not_booked(self):
        self.assertEqual(D.c10(CASE, art([("x", OFFER, 2)], [VERIFY, SEARCH]))["verdict"], "fail")


class ExitDecision(unittest.TestCase):
    def result(self, cid, grades, suite="representative"):
        return {"case_id": cid, "run": 1, "suite": suite, "strata": {"channel": "chat"},
                "grades": [{"claim": c, "verdict": v, "evidence": []} for c, v in grades], "judges": [], "latency_ms": [100]}

    def test_non_compensable_overrides_averages(self):
        rs = [self.result(f"c{i}", [("C1", "pass"), ("C3", "pass"), ("C4", "pass")]) for i in range(50)]
        rs.append(self.result("bad", [("C1", "fail"), ("C3", "pass")]))
        agg = aggregate.aggregate(rs)
        self.assertEqual(agg["decision"], "fail")
        self.assertTrue(any("non-compensable C1" in r for r in agg["reasons"]))

    def test_inconclusive_never_counts_as_pass(self):
        rs = [self.result("a", [("C1", "pass"), ("C5", "inconclusive")])]
        agg = aggregate.aggregate(rs)
        self.assertEqual(agg["claims"]["C5"]["n"], 0)
        self.assertEqual(agg["decision"], "inconclusive")

    def test_challenge_failure_of_noncompensable_also_fails(self):
        rs = [self.result("a", [("C1", "pass")]), self.result("b", [("C2", "fail")], suite="challenge")]
        self.assertEqual(aggregate.aggregate(rs)["decision"], "fail")

    def test_coverage_gap_surfaces_all_inconclusive_stratum(self):
        rs = [self.result(f"ok{i}", [("C1", "pass"), ("C2", "pass")]) for i in range(4)]
        for i in range(2):
            r = self.result(f"gap{i}", [("C1", "pass"), ("C2", "inconclusive")])
            r["strata"] = {"channel": "chat", "phrasing": "ascii_typed"}
            rs.append(r)
        agg = aggregate.aggregate(rs)
        self.assertIn("phrasing=ascii_typed", agg["c9"]["C2"]["coverage_gaps"])

    def test_uncalibrated_judge_never_gates(self):
        r = self.result("a", [("C1", "pass")])
        r["judges"] = [{"claim": "C6:judge", "verdict": "fail"}]
        self.assertEqual(aggregate.aggregate([r])["decision"], "pass")

    def test_wilson(self):
        lo, hi = aggregate.wilson(9, 10)
        self.assertAlmostEqual(lo, 0.596, places=2)
        self.assertAlmostEqual(hi, 0.982, places=2)


if __name__ == "__main__":
    unittest.main()
