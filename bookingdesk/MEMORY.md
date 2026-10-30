# Scheduling preference memory

The store holds independent patient and provider profiles in one SQLite database.
Reads use exact subject IDs. A composite primary key on kind, subject, scope and
preference key supports targeted access; conversations are not the memory database.
The service is stdlib-only and independent of the model adapter.

Each preference has a typed value and its own `hard` (filter) or `soft` (ranking)
strength. Standing preferences survive calls. An optional request scope overlays
the same patient's standing value for that key; it never overrides clinic rules
or a provider's hard constraints. Expiry dates are inclusive local calendar dates;
callers supply the clinic date on reads. Null removes a scoped entry, revealing
the standing value again. No other fields are changed by a patch.

`read(kind, subject, request_id, on_date)` returns the effective preferences,
provenance records and profile version. `patch(..., expected_version, source,
actor)` atomically updates records, increments the subject version, and writes a
change history. Stale writers must fetch and reconcile rather than overwrite.
`seed` initializes a subject only once; it preserves staff changes on restart.

Authorization belongs to the application boundary: derive the patient ID from
the verified session; only the staff interface can update provider profiles.
The store is not an authentication or tenant-isolation service. A production
deployment must add organization scoping and authenticated object permissions.
Change history contains preference values and must have the same protection and
retention policy as the preference records, separate from application logs.

Live availability, bookings, referral eligibility and clinic policy remain in
their authoritative services. Fetch them when scheduling and revalidate before
booking. A preference snapshot is not a slot reservation.

Run the storage checks from this directory:

```sh
python3 -m unittest discover -s tests -v
```
