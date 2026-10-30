"""Seed data: mock Epic Twin chart (FHIR-shaped), the clinic's scheduling
procedure, and the two Notion-style desks (Available Times, Patient Requests).
Everything resets to this on /api/reset."""
import copy
import hashlib
from datetime import datetime, timedelta

# The demo clock. Times are Pacific, stored naive.
NOW = datetime(2026, 10, 30, 9, 0)

PATIENTS = [
    {
        "resourceType": "Patient",
        "id": "pmlab-f-001-df8d27e9-pat",
        "name": [{"given": ["Riley"], "family": "Jordan"}],
        "birthDate": "1987-03-14",
        "telecom": [{"system": "phone", "value": "415-555-0142"}],
        "address": [{"city": "San Francisco", "state": "CA"}],
    },
    {
        "resourceType": "Patient",
        "id": "pmlab-f-002-11aa22bb-pat",
        "name": [{"given": ["Riley"], "family": "Jordan"}],
        "birthDate": "1962-11-02",
        "telecom": [{"system": "phone", "value": "650-555-0199"}],
        "address": [{"city": "Oakland", "state": "CA"}],
    },
]

# Five more demo callers, each set up to show a different path. Synthetic, made up for the demo.
#   (name, DOB, phone, city, scenario)
DEMO_CALLERS = [
    ("bkd-002", "Avery", "Collins", "1994-07-22", "510-555-0117", "Berkeley"),   # new referral, Eastside
    ("bkd-003", "Morgan", "Lee", "1979-01-09", "415-555-0163", "San Francisco"),  # eval done: follow-ups allowed
    ("bkd-004", "Harold", "Chen", "1958-05-30", "650-555-0181", "Daly City"),     # referral expired
    ("bkd-005", "Nora", "Bennett", "1990-12-03", "408-555-0136", "San Jose"),     # already booked: reschedule
    ("bkd-006", "Sam", "Patel", "2001-09-17", "925-555-0174", "Walnut Creek"),    # no referral on file
]
PATIENTS += [
    {"resourceType": "Patient", "id": f"{pid}-pat", "name": [{"given": [given], "family": family}],
     "birthDate": dob, "telecom": [{"system": "phone", "value": phone}], "address": [{"city": city, "state": "CA"}]}
    for pid, given, family, dob, phone, city in DEMO_CALLERS
]


def _referral(pid, name, date, provider, dx, order, visits, valid_from, valid_to, done, notes):
    return {
        "documentReference": f"{pid}-doc-000",
        "binary": f"{pid}-bin-000",
        "date": date,
        "type": "Referral to Physical Therapy",
        "text": (
            "REFERRAL TO PHYSICAL THERAPY\n"
            f"Date: {date}\n"
            f"Referring provider: {provider}\n"
            f"Patient: {name}\n"
            f"Diagnosis: {dx}\n"
            f"Order: {order}\n"
            f"Authorized visits: {visits}\n"
            f"Referral valid: {valid_from} through {valid_to}\n"
            f"Notes: {notes}"
        ),
        "authorized_visit_types": ["PT Evaluation", "PT Follow-up"],
        "valid_from": valid_from,
        "valid_to": valid_to,
        "completed_evaluation": done,
    }


# DocumentReference -> Binary, flattened.
REFERRALS = {
    "pmlab-f-001-df8d27e9-pat": {
        "documentReference": "pmlab-f-001-df8d27e9-doc-000",
        "binary": "pmlab-f-001-df8d27e9-bin-000",
        "date": "2026-10-27",
        "type": "Referral to Physical Therapy",
        "text": (
            "REFERRAL TO PHYSICAL THERAPY\n"
            "Date: 2026-10-27\n"
            "Referring provider: Priya Shah, MD (Orthopedics)\n"
            "Patient: Riley Jordan\n"
            "Diagnosis: Right knee pain, s/p meniscus irritation (M23.91)\n"
            "Order: PT evaluation and treatment, 2x/week x 6 weeks\n"
            "Authorized visits: 1 evaluation + up to 12 follow-ups\n"
            "Referral valid: 2026-10-27 through 2026-12-27\n"
            "Notes: Begin with initial evaluation. Patient prefers text messages."
        ),
        "authorized_visit_types": ["PT Evaluation", "PT Follow-up"],
        "valid_from": "2026-10-27",
        "valid_to": "2026-12-27",
        "completed_evaluation": False,
    },
    "bkd-002-pat": _referral(
        "bkd-002", "Avery Collins", "2026-10-26", "Elena Ruiz, MD (Sports Medicine)",
        "Left ankle sprain, grade II (S93.402A)", "PT evaluation and treatment, 2x/week x 4 weeks",
        "1 evaluation + up to 8 follow-ups", "2026-10-26", "2026-12-26", False,
        "Begin with initial evaluation. Patient lives near the Eastside clinic."),
    "bkd-003-pat": _referral(
        "bkd-003", "Morgan Lee", "2026-10-01", "Priya Shah, MD (Orthopedics)",
        "Chronic low back pain (M54.50)", "PT treatment, 2x/week x 8 weeks",
        "1 evaluation + up to 16 follow-ups", "2026-10-01", "2026-12-31", True,
        "Initial evaluation completed 2026-10-20 with Dana Kim, PT. Continue follow-ups."),
    "bkd-004-pat": _referral(
        "bkd-004", "Harold Chen", "2026-08-01", "James Okafor, MD (Family Medicine)",
        "Right shoulder impingement (M75.41)", "PT evaluation and treatment, 2x/week x 6 weeks",
        "1 evaluation + up to 12 follow-ups", "2026-08-01", "2026-10-15", False,
        "Patient did not schedule during the referral window."),
    "bkd-005-pat": _referral(
        "bkd-005", "Nora Bennett", "2026-10-28", "Priya Shah, MD (Orthopedics)",
        "Post-op rehab, right ACL reconstruction 2026-10-14 (Z47.89)", "PT evaluation and treatment, 3x/week x 8 weeks",
        "1 evaluation + up to 24 follow-ups", "2026-10-28", "2027-01-28", False,
        "Evaluation already scheduled. Patient prefers afternoons."),
    # Sam Patel (bkd-006) has no referral on file on purpose.
}

POLICY = {
    "visit_types": {
        "PT Evaluation": {"minutes": 60, "requires": "an active PT referral"},
        "PT Follow-up": {
            "minutes": 45,
            "requires": "a completed PT Evaluation on file",
        },
    },
    "lead_time_hours": 24,
    "max_days_out": 30,
    "arrive_early_minutes": 15,
    "locations": {
        "Downtown": "450 Market St, Suite 300",
        "Eastside": "2100 E 14th St",
    },
    "rules": [
        "A new referral starts with a PT Evaluation. Follow-ups only after the evaluation is completed.",
        "Book no sooner than 24 hours from now and no later than 30 days out, and inside the referral window.",
        "Ask patients to arrive 15 minutes early and bring ID and insurance card.",
        "Cancellations and reschedules need the patient's explicit confirmation before the old slot is released.",
    ],
}

PROVIDER_PREFS = {
    "Dana Kim, PT": {
        "hard": [
            "No back-to-back evaluations (an evaluation cannot start within 3 hours of another booked evaluation).",
            "Friday afternoons are reserved for post-op patients.",
        ],
        "soft": ["Prefers evaluations before noon."],
    },
    "Marcus Webb, PT": {
        "hard": ["No back-to-back evaluations (an evaluation cannot start within 3 hours of another booked evaluation)."],
        "soft": ["Prefers evaluations on Tuesdays and Thursdays."],
    },
}


def _slots():
    """Two weeks of weekday slots across two providers and two locations."""
    out = []
    times = [(8, 0), (9, 30), (11, 0), (13, 0), (14, 30), (16, 0)]
    day = datetime(2026, 11, 2)
    n = 0
    while day <= datetime(2026, 11, 13):
        if day.weekday() < 5:
            for prov, loc in (("Dana Kim, PT", "Downtown"), ("Marcus Webb, PT", "Eastside")):
                # Marcus covers Downtown on Tue/Thu.
                if prov == "Marcus Webb, PT" and day.weekday() in (1, 3):
                    loc = "Downtown"
                for i, (h, m) in enumerate(times):
                    start = day.replace(hour=h, minute=m)
                    key = f"{prov}{start.isoformat()}".encode()
                    r = int(hashlib.md5(key).hexdigest(), 16) % 10
                    vt = "PT Evaluation" if i % 2 == 0 else "PT Follow-up"
                    status = "Booked" if r < 2 else "Available"
                    n += 1
                    out.append({
                        "id": f"S{n:03d}",
                        "start": start.isoformat(timespec="minutes"),
                        "minutes": POLICY["visit_types"][vt]["minutes"],
                        "provider": prov,
                        "location": loc,
                        "visit_type": vt,
                        "status": status,
                        "booked_for": None,
                    })
        day += timedelta(days=1)
    return out


REQUESTS = [
    {
        "id": "R001",
        "patient_name": "Riley Jordan",
        "patient_id": "pmlab-f-001-df8d27e9-pat",
        "need": "PT evaluation per orthopedics referral (2026-10-27)",
        "status": "New",
        "phone": "415-555-0142",
        "preferred_times": "",
        "patient_preferences": None,
        "selected_slot": None,
        "next_action": "",
        "notes": [],
        "review": None,
    }
]


def _request(rid, pid, name, phone, need, status="New", prefs=None, notes=None):
    return {"id": rid, "patient_name": name, "patient_id": f"{pid}-pat", "need": need, "status": status,
            "phone": phone, "preferred_times": "", "patient_preferences": prefs, "selected_slot": None,
            "next_action": "", "notes": notes or [], "review": None, "preference_history": [], "last_search": None}


REQUESTS += [
    _request("R002", "bkd-002", "Avery Collins", "510-555-0117", "PT evaluation per sports medicine referral (2026-10-26)"),
    _request("R003", "bkd-003", "Morgan Lee", "415-555-0163", "PT follow-ups (evaluation completed 2026-10-20)",
             prefs={"time_of_day": "morning", "location": "Downtown", "provider": "Dana Kim, PT",
                    "notes": "Likes to keep the same therapist."}),
    _request("R004", "bkd-004", "Harold Chen", "650-555-0181", "PT evaluation per family medicine referral (2026-08-01)"),
    _request("R005", "bkd-005", "Nora Bennett", "408-555-0136", "PT evaluation after ACL reconstruction", status="Booked",
             prefs={"time_of_day": "afternoon", "location": "Eastside", "notes": "Post-op; rides with a family member."}),
    _request("R006", "bkd-006", "Sam Patel", "925-555-0174", "Called about PT for a shoulder; no referral received yet"),
]


def _book_nora(slots, requests):
    """Nora starts with an evaluation already booked so the reschedule flow has something to move."""
    sl = next(s for s in slots if s["status"] == "Available" and s["visit_type"] == "PT Evaluation"
              and s["location"] == "Eastside" and s["start"] >= "2026-11-04" and int(s["start"][11:13]) >= 12)
    sl["status"], sl["booked_for"] = "Booked", "bkd-005-pat"
    req = next(r for r in requests if r["id"] == "R005")
    req["selected_slot"] = sl["id"]
    req["notes"].append(f"2026-10-29 Front desk booked {sl['id']} (evaluation, {sl['provider']}, Eastside).")


def seed():
    slots, requests = _slots(), copy.deepcopy(REQUESTS)
    _book_nora(slots, requests)
    return {
        "patients": copy.deepcopy(PATIENTS),
        "referrals": copy.deepcopy(REFERRALS),
        "policy": copy.deepcopy(POLICY),
        "provider_prefs": copy.deepcopy(PROVIDER_PREFS),
        "slots": slots,
        "requests": requests,
        "outbox": [],
        "corrections": [],
        "candidate_rules": [],
    }
