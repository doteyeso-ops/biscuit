"""Google's own answer, read out of the user's real Brave session over CDP.

Why CDP and not a fresh headless launch: a clean profile gets Google's
"unusual traffic" interstitial (measured: 5 KB page, no results), while the
signed-in profile gets the real page with the AI Overview. So Brave is started
once with --remote-debugging-port=9222 and we drive a tab it owns.

Google already writes the short answer; nothing has to be generated. Verified:
Mount Everest -> "8,848.86 meters (29,031.7 feet) ... announced by Nepal and
China in 2020".
"""

import asyncio
import json
import re
import urllib.parse
import urllib.request

CDP = "http://127.0.0.1:9222"
_STOPS = ("Show more", "Would you like", "Sources", "View related", "Learn more",
          "People also ask", "Top stories", "Videos", "Images", "Shopping",
          "AI Mode", "About this page")


def cdp_alive():
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=3) as r:
            return bool(json.loads(r.read()).get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _new_tab(url):
    """Open a tab; returns (id, ws url). Newer Chrome builds want PUT."""
    q = f"{CDP}/json/new?{urllib.parse.quote(url, safe='')}"
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(q, method=method)
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read())
            if j.get("webSocketDebuggerUrl"):
                return j.get("id"), j["webSocketDebuggerUrl"]
        except Exception:
            continue
    raise RuntimeError("could not open a tab via CDP")


def _close_tab(tab_id):
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tab_id}", timeout=5).read()
    except Exception:
        pass


def _extract(text):
    i = text.find("AI Overview")
    if i < 0:
        i = text.find("AI Mode replied")
    if i < 0:
        return ""
    body = text[i:]
    body = re.sub(r"^AI Overview\s*(AI Mode replied:)?\s*", "", body)
    body = body.replace("[Highlight start]", "").replace("[Highlight end]", "")
    for stop in _STOPS:
        j = body.find(stop)
        if 80 < j:
            body = body[:j]
    # Google appends source chips and follow-up bullets ("Wikipedia +2",
    # "Series MVP: ..."). Spoken aloud they are noise, so keep the prose only.
    body = re.split(r"\s\+\d", body)[0]
    # Source attribution ("... Service (.gov)", "ESPN") is not speech.
    body = re.split(r"\(?\.(?:gov|com|org|net|edu|co\.uk)\)?", body)[0]
    body = re.split(r"\s[·|]\s", body)[0]
    body = re.sub(r"\s+", " ", body).strip()
    body = "".join(c for c in body if ord(c) >= 32)
    body = re.sub(r"\s+([.,;:!?])", lambda m: m.group(1), body)
    # Drop the outlet name that precedes a source domain ("National
    # Environmental Satellite ... (.gov)"), not just the domain itself.
    body = re.split(r"\s[A-Z][^.!?]{3,70}\(\.(?:gov|com|org|net|edu)\)", body)[0]
    # A trailing source name ("Wikipedia", "ESPN") has no terminal punctuation;
    # keeping only real sentences drops it without a list of outlet names.
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", body) if x.strip()]
    kept = [x for x in sents if x[-1] in ".!?"]
    sents = kept or sents          # never return empty for this reason
    body = " ".join(sents[:2]).strip()
    if len(body) > 420:
        cut = body[:420]
        dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        body = cut[:dot + 1] if dot > 80 else cut.rsplit(" ", 1)[0] + "."
    return body.strip()


async def google_answer(q, timeout=9.0, settle=0.5):
    """(answer, kind) from the user's Brave. Empty answer when unavailable."""
    if not cdp_alive():
        return "", "cdp down"
    import aiohttp

    url = "https://www.google.com/search?hl=en&q=" + urllib.parse.quote(q)
    try:
        tab_id, ws_url = await asyncio.to_thread(_new_tab, "about:blank")
    except Exception as e:
        return "", f"tab failed: {e}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url, timeout=10) as ws:
                mid = {"n": 0}

                async def cmd(method, params=None):
                    mid["n"] += 1
                    await ws.send_str(json.dumps(
                        {"id": mid["n"], "method": method,
                         "params": params or {}}))

                await cmd("Page.enable")
                await cmd("Runtime.enable")
                text = ""
                for attempt in range(1):
                    await cmd("Page.navigate", {"url": url})
                    end = asyncio.get_event_loop().time() + timeout
                    while asyncio.get_event_loop().time() < end:
                        await asyncio.sleep(settle)
                        mid["n"] += 1
                        await ws.send_str(json.dumps({
                            "id": mid["n"], "method": "Runtime.evaluate",
                            "params": {
                                "expression": "document.readyState+'|'"
                                              "+(document.body?document.body.innerText:'')",
                                "returnByValue": True}}))
                        mine = mid["n"]
                        got = None
                        while True:
                            msg = await asyncio.wait_for(ws.receive(), 8)
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            j = json.loads(msg.data)
                            if j.get("id") == mine:
                                got = j
                                break
                        if not got:
                            continue
                        val = (((got.get("result") or {}).get("result") or {})
                               .get("value") or "")
                        state, _, text = val.partition("|")
                        if state != "complete":
                            continue
                        if "unusual traffic" in text:
                            return "", "google blocked"
                        if "AI Overview" in text:
                            return _extract(text), "google_ai"
                    if "unusual traffic" in text:
                        break
                    # rendered but no overview yet: one reload usually fixes it
                return _extract(text), "no overview"
    except Exception as e:
        return "", f"cdp error: {e}"
    finally:
        await asyncio.to_thread(_close_tab, tab_id)


if __name__ == "__main__":
    async def main():
        import time
        for q in ("how tall is mount everest", "who won the last world series",
                  "what causes thunder"):
            t = time.monotonic()
            txt, kind = await google_answer(q)
            print(f"[{time.monotonic()-t:5.2f}s {kind}] {q}\n    -> {txt[:220]}\n")

    asyncio.run(main())
