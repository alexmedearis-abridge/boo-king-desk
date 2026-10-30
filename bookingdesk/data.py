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
    }
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


def seed():
    return {
        "patients": copy.deepcopy(PATIENTS),
        "referrals": copy.deepcopy(REFERRALS),
        "policy": copy.deepcopy(POLICY),
        "provider_prefs": copy.deepcopy(PROVIDER_PREFS),
        "slots": _slots(),
        "requests": copy.deepcopy(REQUESTS),
        "outbox": [],
        "corrections": [],
        "candidate_rules": [],
    }
