import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory import PreferenceStore, VersionConflict


def pref(value, strength="hard"):
    return {"value": value, "strength": strength}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.store = PreferenceStore(self.path)

    def patch(self, changes, version, **kwargs):
        return self.store.patch("patient", "p1", changes, version, source="patient_explicit", actor="patient:p1", **kwargs)

    def test_restart_partial_update_and_subject_isolation(self):
        self.patch({"location": pref("Downtown"), "time_of_day": pref("afternoon", "soft")}, 0)
        self.patch({"location": pref("Eastside")}, 1)
        saved = PreferenceStore(self.path).read("patient", "p1")
        self.assertEqual(saved["effective"]["time_of_day"], pref("afternoon", "soft"))
        self.assertEqual(saved["effective"]["location"], pref("Eastside"))
        self.assertEqual(self.store.read("patient", "p2")["effective"], {})

    def test_request_overlay_expiry_and_delete(self):
        self.patch({"location": pref("Downtown")}, 0)
        self.patch({"location": pref("Eastside")}, 1, request_id="r1", valid_until="2026-11-05")
        self.assertEqual(self.store.read("patient", "p1", "r1", "2026-11-05")["effective"]["location"], pref("Eastside"))
        self.assertEqual(self.store.read("patient", "p1", "r2", "2026-11-05")["effective"]["location"], pref("Downtown"))
        self.assertEqual(self.store.read("patient", "p1", "r1", "2026-11-06")["effective"]["location"], pref("Downtown"))
        self.patch({"location": None}, 2, request_id="r1")
        self.assertEqual(self.store.read("patient", "p1", "r1")["effective"]["location"], pref("Downtown"))

    def test_stale_update_and_seed_do_not_overwrite(self):
        self.patch({"location": pref("Eastside")}, 0)
        with self.assertRaises(VersionConflict):
            self.patch({"time_of_day": pref("morning")}, 0)
        self.store.seed("patient", "p1", {"location": pref("Downtown")})
        self.assertEqual(self.store.read("patient", "p1")["version"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM preference_changes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
