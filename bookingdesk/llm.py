"""Model backends, stdlib only.

History is kept in a neutral format and converted per call:
  {"role": "user", "content": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool", "results": [{"id", "content"}]}

Backend is picked from the environment:
  ANTHROPIC_API_KEY set  -> Anthropic Messages API (MODEL, default claude-sonnet-5-5)
  LLM_BASE_URL set       -> any OpenAI-compatible /chat/completions endpoint,
                            e.g. the Abridge gateway (MODEL required, LLM_API_KEY optional)
"""
import json
import os
import urllib.error
import urllib.request


def _post(url, body, headers):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode()[:500]}")


def backend_name():
    if os.environ.get("LLM_BACKEND") == "fake":
        return "fake (scripted, plumbing only)"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"anthropic:{os.environ.get('MODEL', 'claude-sonnet-5-5')}"
    if os.environ.get("LLM_BASE_URL"):
        return f"openai-compatible:{os.environ.get('MODEL', '?')} @ {os.environ['LLM_BASE_URL']}"
    return None


def call(system, history, tools, temperature=None, model=None):
    """Returns {"text": str, "tool_calls": [...]}. temperature/model are for eval judges; the agent omits them."""
    if os.environ.get("LLM_BACKEND") == "fake":
        import fake_llm
        return fake_llm.call(system, history, tools)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _anthropic(system, history, tools, temperature, model)
    if os.environ.get("LLM_BASE_URL"):
        return _openai(system, history, tools, temperature, model)
    raise RuntimeError("No model configured. Set ANTHROPIC_API_KEY, or LLM_BASE_URL + MODEL.")


def _anthropic(system, history, tools, temperature=None, model=None):
    msgs = []
    for h in history:
        if h["role"] == "user":
            msgs.append({"role": "user", "content": h["content"]})
        elif h["role"] == "assistant":
            blocks = []
            if h.get("text"):
                blocks.append({"type": "text", "text": h["text"]})
            for tc in h.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            msgs.append({"role": "assistant", "content": blocks})
        else:
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in h["results"]
            ]})
    body = {
        "model": model or os.environ.get("MODEL", "claude-sonnet-5-5"),
        "max_tokens": 2048,
        "system": system,
        "messages": msgs,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
    r = _post(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages",
        body,
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
    )
    text = "".join(b["text"] for b in r["content"] if b["type"] == "text")
    calls = [{"id": b["id"], "name": b["name"], "input": b["input"]} for b in r["content"] if b["type"] == "tool_use"]
    return {"text": text, "tool_calls": calls}


def _gateway_headers():
    # Abridge gateway (per the hackathon's verified config): the key is a placeholder,
    # and the caller-service header identifies the app.
    return {
        "authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'not-needed')}",
        "Abridge-CallerService": os.environ.get("CALLER_SERVICE", "boo-king-desk"),
    }


def list_models():
    """GET /models on the OpenAI-compatible endpoint, for picking MODEL."""
    req = urllib.request.Request(os.environ["LLM_BASE_URL"].rstrip("/") + "/models", headers=_gateway_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return sorted(m["id"] for m in json.loads(r.read()).get("data", []))


VOICE_STYLE = ("Warm, relaxed front-desk receptionist at a physical therapy clinic, on a phone call. "
               "Natural pace, friendly, a light smile in the voice.")
DEFAULT_VOICE = "tts:gpt-4o-mini-tts-2025-12-15:marin"


def speakable(text):
    """Turn a chat reply into something that reads well aloud."""
    import re
    t = re.sub(r"[*_`#]+", "", text)                  # markdown
    t = re.sub(r"([^.!?:,])[ \t]*\n\s*\n", r"\1. ", t)  # paragraph break = sentence end
    t = re.sub(r"\s*\n\s*", " ", t)
    t = re.sub(r"\bPT\b", "P.T.", t)
    return re.sub(r"\.\s*\.", ".", t).strip()


def tts_stream(text, voice=DEFAULT_VOICE):
    """Yield mp3 chunks for a reply. `voice` is "<engine>:<model>:<voice>":
    tts   -> /audio/speech, streamed as it is generated
    audio -> gpt-audio via /chat/completions (more natural, slower, returned whole)"""
    engine, model, name = (voice or DEFAULT_VOICE).split(":", 2)
    text = speakable(text)
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    headers = {"content-type": "application/json", **_gateway_headers()}
    if engine == "audio":
        import base64
        body = {"model": model, "modalities": ["text", "audio"], "audio": {"voice": name, "format": "mp3"},
                "messages": [{"role": "system", "content": VOICE_STYLE + " Read the user's text aloud exactly as written, word for word. Do not add or change anything."},
                             {"role": "user", "content": text}]}
        r = _post(base + "/chat/completions", body, headers)
        yield base64.b64decode(r["choices"][0]["message"]["audio"]["data"])
        return
    body = {"model": model, "voice": name, "input": text, "instructions": VOICE_STYLE, "response_format": "mp3"}
    req = urllib.request.Request(base + "/audio/speech", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        while True:
            chunk = r.read(8192)
            if not chunk:
                break
            yield chunk


def _openai(system, history, tools, temperature=None, model=None):
    msgs = [{"role": "system", "content": system}]
    for h in history:
        if h["role"] == "user":
            msgs.append({"role": "user", "content": h["content"]})
        elif h["role"] == "assistant":
            m = {"role": "assistant", "content": h.get("text") or None}
            if h.get("tool_calls"):
                m["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])}}
                    for tc in h["tool_calls"]
                ]
            msgs.append(m)
        else:
            for r in h["results"]:
                msgs.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
    body = {"model": model or os.environ.get("MODEL"), "messages": msgs}
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
    r = _post(os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions", body, _gateway_headers())
    m = r["choices"][0]["message"]
    calls = []
    for tc in m.get("tool_calls") or []:
        args = tc["function"].get("arguments") or "{}"
        calls.append({"id": tc["id"], "name": tc["function"]["name"],
                      "input": json.loads(args) if isinstance(args, str) else args})
    return {"text": m.get("content") or "", "tool_calls": calls}
