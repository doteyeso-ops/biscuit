"""Settings and history for the Dot, plus the dashboard page.

One JSON file holds the user-tunable settings (EQ, follow-ups, favourites) so
the controller and the music pipeline read the same values without another
service. History is append-only and trimmed, so the page can show what was
played recently and replay it with one click.
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(ROOT, "dot_config.json")
HISTORY = os.path.join(ROOT, "music_history.json")

DEFAULTS = {
    "eq": {"bass": 0, "mid": 0, "treble": 0},   # dB, -12..+12
    "followups": 0,          # extra turns after one press
    "always_on": False,      # wake-word listening
    "volume": 60,
    "favourites": [],
}


def _read(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(_read(CONFIG, {}) or {})
    cfg["eq"] = {**DEFAULTS["eq"], **(cfg.get("eq") or {})}
    return cfg


def save_config(cfg):
    merged = load_config()
    merged.update(cfg or {})
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1)
    return merged


def add_history(title, query):
    rows = _read(HISTORY, []) or []
    rows.insert(0, {"title": title, "query": query, "at": int(time.time())})
    # dedupe by title, keep the newest, cap at 100
    seen, out = set(), []
    for r in rows:
        if r.get("title") in seen:
            continue
        seen.add(r.get("title"))
        out.append(r)
    out = out[:100]
    try:
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    except OSError:
        pass
    return out


def history(limit=30):
    return (_read(HISTORY, []) or [])[:limit]


def eq_filter():
    """ffmpeg filter chain for the current EQ, or "" when flat."""
    eq = load_config().get("eq") or {}
    parts = []
    for band, freq, width in (("bass", 120, "1.0"),
                              ("mid", 1000, "1.2"),
                              ("treble", 8000, "1.0")):
        g = float(eq.get(band) or 0)
        if abs(g) >= 0.5:
            parts.append(f"equalizer=f={freq}:width_type=o:width={width}:g={g}")
    return ",".join(parts)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Hermes Dot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e1116;--card:#171c24;--fg:#e8edf5;--dim:#8b97a8;--acc:#4cc2ff;--ok:#39d98a;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:18px 22px;border-bottom:1px solid #222a35;display:flex;align-items:baseline;gap:12px}
h1{font-size:18px;margin:0;font-weight:600}
.pill{font-size:12px;padding:3px 9px;border-radius:999px;background:#222a35;color:var(--dim)}
.pill.on{background:#123a2a;color:var(--ok)}
.pill.off{background:#3a1a1a;color:var(--bad)}
main{padding:18px 22px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--card);border:1px solid #222a35;border-radius:12px;padding:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0 0 12px}
.row{display:flex;gap:10px;align-items:center}
input[type=text]{flex:1;background:#0e1116;border:1px solid #2a3441;color:var(--fg);border-radius:8px;padding:10px 12px}
button{background:var(--acc);color:#06202e;border:0;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}
button.ghost{background:#222a35;color:var(--fg)}
button:disabled{opacity:.45;cursor:default}
.song{font-size:17px;font-weight:600;margin:2px 0 4px}
.dim{color:var(--dim);font-size:13px}
ul{list-style:none;margin:0;padding:0;max-height:260px;overflow:auto}
li{padding:8px 6px;border-bottom:1px solid #1d2530;display:flex;justify-content:space-between;gap:10px;cursor:pointer}
li:hover{background:#1d2530}
label.eq{display:grid;grid-template-columns:60px 1fr 44px;gap:10px;align-items:center;margin:8px 0}
input[type=range]{width:100%}
input[type=range]::-webkit-slider-thumb{appearance:none;width:16px;height:16px;border-radius:50%;background:var(--acc)}
input[type=range]{appearance:none;height:4px;background:#2a3441;border-radius:2px}
.toggle{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1d2530}
.sw{width:44px;height:24px;background:#2a3441;border-radius:999px;position:relative;cursor:pointer;transition:.15s}
.sw.on{background:var(--acc)}
.sw i{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.15s}
.sw.on i{left:23px}
</style></head><body>
<header><h1>Hermes Dot</h1><span id="dev" class="pill">connecting…</span><span id="mode" class="pill">—</span></header>
<main>
  <section>
    <h2>Now playing</h2>
    <div id="song" class="song">Nothing playing</div>
    <div id="hint" class="dim">Press the Dot's button and say “play …”</div>
    <div class="row" style="margin-top:14px">
      <input id="q" type="text" placeholder="Song, artist or anything…">
      <button onclick="play()">Play</button>
      <button class="ghost" onclick="stop()">Stop</button>
    </div>
  </section>
  <section>
    <h2>Volume</h2>
    <div class="row"><input id="vol" type="range" min="0" max="100" oninput="setVol(this.value)"><span id="volv" class="dim"></span></div>
    <h2 style="margin-top:18px">Behaviour</h2>
    <div class="toggle"><span>Follow-up turns after a press<br><span class="dim">off = one press, one answer</span></span><div id="fu" class="sw" onclick="toggle('followups')"><i></i></div></div>
    <div class="toggle"><span>Always-on wake word<br><span class="dim">“Hermes …” without pressing</span></span><div id="ao" class="sw" onclick="toggle('always_on')"><i></i></div></div>
  </section>
  <section>
    <h2>Equaliser</h2>
    <label class="eq">Bass<input id="bass" type="range" min="-12" max="12" step="1" oninput="eq()"><span id="bassv"></span></label>
    <label class="eq">Mid<input id="mid" type="range" min="-12" max="12" step="1" oninput="eq()"><span id="midv"></span></label>
    <label class="eq">Treble<input id="treble" type="range" min="-12" max="12" step="1" oninput="eq()"><span id="treblev"></span></label>
    <div class="dim" style="margin-top:8px">Applies to music from the next song.</div>
  </section>
  <section>
    <h2>Recently played</h2>
    <ul id="hist"></ul>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function play(){const q=$('#q').value.trim(); if(q) api('/api/play',{q}); }
function stop(){api('/api/stop',{});}
function setVol(v){$('#volv').textContent=v; api('/api/volume',{level:+v});}
function eq(){for(const b of ['bass','mid','treble']){$('#'+b+'v').textContent=$('#'+b).value+' dB';}
  api('/api/eq',{bass:+$('#bass').value,mid:+$('#mid').value,treble:+$('#treble').value});}
function toggle(k){api('/api/config',{[k]:$('#'+(k==='followups'?'fu':'ao')).classList.contains('on')?0:1});}
async function refresh(){
  const s=await api('/api/state');
  $('#dev').textContent=s.device?'Dot connected':'Dot offline';
  $('#dev').className='pill '+(s.device?'on':'off');
  $('#mode').textContent=s.playing?'playing':'idle';
  $('#song').textContent=s.now_playing||'Nothing playing';
  $('#vol').value=s.volume; $('#volv').textContent=s.volume;
  for(const b of ['bass','mid','treble']){$('#'+b).value=s.eq[b]; $('#'+b+'v').textContent=s.eq[b]+' dB';}
  $('#fu').className='sw'+(s.followups?' on':''); $('#ao').className='sw'+(s.always_on?' on':'');
  $('#hist').innerHTML=(s.history||[]).map(h=>`<li onclick="api('/api/play',{q:'${(h.query||'').replace(/'/g,'')}'})"><span>${h.title}</span><span class="dim">${new Date(h.at*1000).toLocaleTimeString()}</span></li>`).join('')||'<li class="dim">Nothing yet</li>';
}
refresh(); setInterval(refresh,4000);
</script></body></html>"""
