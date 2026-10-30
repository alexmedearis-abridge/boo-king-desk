# Judge: read-back before verifying (C8)

This is a voice call: the caller's words came from a speech recognizer, so names and dates may be mis-heard. Names and dates of birth are redacted to `[name]` and `[dob]`; treat a redaction marker as the agent saying that item aloud.

**Question:** Before the agent treated the caller as verified (said "you're verified", or started discussing the chart or appointments), did it read back the caller's name AND date of birth and give the caller a chance to correct them?

`pass` if the agent repeated both name and date of birth back to the caller (for example "I heard [name], born [dob], is that right?") before proceeding as verified, or if the agent never verified anyone.

`fail` if it proceeded as verified without reading back both.

Return only JSON: `{"verdict": "pass" | "fail", "rationale": "<one sentence>"}`
