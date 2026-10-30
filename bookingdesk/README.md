# Boo-king Desk: live demo

A Hyro-style scheduling agent for Riley Jordan's PT evaluation. Python 3.9+ standard library only: no pip, no npm.

## Run it

Pick one model backend, then start the server and open http://localhost:8765.

**Option A: Abridge LLM Gateway** (needs VNet / dev network access)

```bash
export LLM_BASE_URL=https://llm-gateway-evals.abridge.coffee/v1
python3 server.py --models
```

Tested and working (no API key needed, just Teleport):

```bash
MODEL=anthropic/claude-sonnet-5-5 LLM_BASE_URL=https://llm-gateway-evals.abridge.coffee/v1 python3 server.py
```

You can also switch models from the page: click the ⚙ label in the header.

The gateway gets `Authorization: Bearer not-needed` and `Abridge-CallerService: boo-king-desk`, matching the hackathon's verified config. Override with `LLM_API_KEY` / `CALLER_SERVICE`.

**Option B: Anthropic API key**

```bash
ANTHROPIC_API_KEY=sk-ant-... python3 server.py
```

Defaults to `claude-sonnet-5-5`. Set `MODEL` to change it.

## Screen layout

- **Patient call** (left): type as Riley, click a scenario chip to prefill a line, or use 🎤 (Chrome). Tick "speak replies" for voice out.
- **Agent trace** (middle): every model step and tool call, with its reason, args, result and latency. DOB and phone are redacted. The same lines go to `traces/<call>.jsonl`. "System prompt" shows the live prompt.
- **Staff desk** (right): Patient Requests (Approve / Correct), Available Times (Flip a slot for the race), SMS outbox, and the Learning tab (corrections → `propose-rules` → approve).

## 5-minute script

1. **Happy path** (chips in order): Open → Identify → Prefs → Pick first. The trace shows find_patient → get_referral → get_policy → get_preferences → search_slots → book_slot → send_message → save_patient_preferences.
2. **Wrong DOB**: New call → Wrong DOB, twice. You see 0 matches, nothing disclosed, then escalate; R002 is set to Waiting.
3. **Slot race**: New call → Identify → Prefs. When the agent offers slots, open Available Times and Flip the first one. Then Pick first: book_slot returns a conflict, the agent searches again and re-offers.
4. Optional: Follow-up ask (blocked by the referral/policy), Change of mind (new search), Red flag (guardrail plus escalate), Reschedule → Yes (second act).
5. Reset data between rehearsals. Tick "dry run" to rehearse with no writes.

## What's mocked

- **Epic Twin**: `data.py` holds a FHIR-shaped Patient and referral (DocumentReference → Binary). The Riley DOB here (1987-03-14) is made up. To use the real chart, swap `t_find_patient` / `t_get_referral` in `agent.py` for GETs to the Twin URLs (VNet required).
- **Notion**: Available Times and Patient Requests are kept in memory with the same fields as the Front Office Desks example. Notion-backed tools can replace them behind the same tool names.
- **SMS**: an outbox panel.
