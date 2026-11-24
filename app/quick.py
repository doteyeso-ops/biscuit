"""Free fast-answer lane for the Dot.

  * facts:  DuckDuckGo (ddgs) - free, no key
  * voice:  Gemini via GOOGLE_API_KEY (free tier), OpenRouter free models as
            a fallback when Google is unavailable or rate limited

Purpose is latency and freshness for general questions. Anything that needs
this house, this laptop, or a real action still goes to the Hermes agent.
"""

import concurrent.futures
import json
import os
import time
import urllib.request

GEMINI_MODEL = os.environ.get("QUICK_GEMINI", "gemini-3-flash-preview")
OPENROUTER_MODEL = os.environ.get("QUICK_MODEL", "google/gemma-4-31b-it:free")
_ENV = os.path.expanduser("~/AppData/Local/hermes/.env")
SEARCH_TIMEOUT = float(os.environ.get("QUICK_SEARCH_TIMEOUT", "2.5"))


def _env(name):
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        with open(_ENV, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith(name + "="):
                    v = ln.split("=", 1)[1].strip().strip('"').strip("'")
                    if v and not v.startswith("*"):
                        return v
    except OSError:
        pass
    return ""


def search(q, n=4):
    from ddgs import DDGS
    rows = []
    with DDGS() as d:
        for r in d.text(q, max_results=n):
            rows.append((r.get("title", ""), r.get("body", "")))
    return rows


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def gemini(prompt, model=None, timeout=20, max_tokens=400):
    key = _env("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("no GOOGLE_API_KEY")
    model = model or GEMINI_MODEL
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.3,
            # Thinking tokens were eating the budget and truncating answers
            # mid-sentence ("Mount Everest is 29,").
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    j = _post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}",
        body, {"Content-Type": "application/json"}, timeout)
    cands = j.get("candidates") or []
    if not cands:
        raise RuntimeError("no candidates")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in parts
                    if isinstance(p, dict) and p.get("text")).strip()


def openrouter(prompt, model=None, timeout=25, max_tokens=200):
    key = _env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY")
    body = {"model": model or OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.3}
    j = _post("https://openrouter.ai/api/v1/chat/completions", body,
              {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}, timeout)
    return (j["choices"][0]["message"]["content"] or "").strip()


def opencode(prompt, model="deepseek-v4.1-flash", timeout=25, max_tokens=500):
    """The model Hermes itself runs on, called directly (~2 s, no CLI boot).

    Needs x-opencode-session or the gateway refuses to route (400
    MissingSessionID), and a token budget that covers its reasoning tokens:
    with max_tokens=80 the content came back empty because reasoning ate it.
    """
    key = _env("OPENCODE_GO_API_KEY")
    if not key:
        raise RuntimeError("no OPENCODE_GO_API_KEY")
    import uuid
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.3}
    j = _post("https://opencode.ai/zen/go/v1/chat/completions", body,
              {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json",
               "User-Agent": "hermes-agent/1.0",
               "x-opencode-session": "hermes-dot-" + uuid.uuid4().hex[:8]},
              timeout)
    return (j["choices"][0]["message"].get("content") or "").strip()


def answer(q, with_search=True):
    """(text, seconds, how) - never raises; the caller falls back to the agent."""
    t0 = time.monotonic()
    ctx = ""
    if with_search:
        try:
            rows = search(q, 4)
            ctx = "\n".join(f"- {t}: {b}" for t, b in rows)
        except Exception:
            ctx = ""
    prompt = f"Question: {q}\n"
    if ctx:
        prompt += f"\nFacts from a web search:\n{ctx}\n"
    prompt += (
        "\nAnswer the way a person would say it out loud: at most two short "
        "sentences, no lists, no preamble, no citations. If the facts do not "
        "answer it, say plainly that you are not sure."
    )
    last = "unknown"
    for name, fn in (("gemini", gemini), ("opencode", opencode),
                     ("openrouter", openrouter)):
        try:
            txt = fn(prompt)
            if txt:
                return txt, time.monotonic() - t0, name
        except Exception as e:
            last = f"{name}: {e}"
    return "", time.monotonic() - t0, f"failed ({last})"


if __name__ == "__main__":
    for q in ("how tall is mount everest",
              "what is the population of nashville tennessee",
              "who won the last world series"):
        txt, secs, how = answer(q)
        print(f"[{secs:5.2f}s {how}] {q}\n    -> {txt[:220]}\n")
