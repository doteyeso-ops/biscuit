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
    "lighting": {"enabled": True, "preset": "equalizer", "hue": 200, "brightness": 1.0},
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
:root{--bg:#080a0e;--panel:#10141b;--line:#1e2632;--fg:#eef3fa;--dim:#7d8ba0;
  --cyan:#37d5ff;--green:#41e08f;--amber:#ffc257;--red:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--fg);
  font:15px/1.45 "Segoe UI",system-ui,sans-serif;
  background:radial-gradient(120% 90% at 50% 0%,#0d1219 0%,#080a0e 62%) fixed}
header{padding:22px 28px 18px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:14px}
h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.18em;text-transform:uppercase}
.pill{font-size:11px;padding:4px 10px;border-radius:999px;background:#141a22;
  color:var(--dim);letter-spacing:.1em;text-transform:uppercase;border:1px solid var(--line)}
.pill.on{background:rgba(55,213,255,.12);color:var(--cyan);border-color:rgba(55,213,255,.35)}
.pill.off{background:rgba(255,107,107,.1);color:var(--red);border-color:rgba(255,107,107,.3)}
main{padding:22px 28px 40px;display:grid;gap:18px;
  grid-template-columns:repeat(auto-fit,minmax(340px,1fr));max-width:1240px;margin:0 auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--dim);
  margin:0 0 14px;font-weight:600}
.row{display:flex;gap:10px;align-items:center}
input[type=text]{flex:1;background:#0a0e13;border:1px solid #222c39;color:var(--fg);
  border-radius:9px;padding:11px 13px;font-size:14px}
input[type=text]:focus{outline:none;border-color:var(--cyan)}
button{background:var(--cyan);color:#06202e;border:0;border-radius:9px;
  padding:11px 16px;font-weight:600;cursor:pointer;font-size:13px;letter-spacing:.04em}
button.ghost{background:#141a22;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:default}
.song{font-size:22px;font-weight:600;margin:2px 0 6px;letter-spacing:-.01em}
.dim{color:var(--dim);font-size:12.5px}
ul{list-style:none;margin:0;padding:0;max-height:280px;overflow:auto}
li{padding:9px 4px;border-bottom:1px solid #161d26;display:flex;justify-content:space-between;
  gap:10px;cursor:pointer;font-size:13.5px;min-width:0}
li span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
li span:last-child{color:var(--dim);font-size:11.5px;flex:0 0 auto;letter-spacing:.04em}
li:last-child{border-bottom:0}
li:hover{background:#141a22}
label.eq{display:grid;grid-template-columns:64px 1fr 54px;gap:12px;align-items:center;
  margin:10px 0;font-size:13px}
input[type=range]{width:100%;appearance:none;height:4px;background:#1b2330;border-radius:2px}
input[type=range]::-webkit-slider-thumb{appearance:none;width:15px;height:15px;
  border-radius:50%;background:var(--cyan);border:2px solid #05070a}
.toggle{display:flex;justify-content:space-between;align-items:center;padding:10px 0;
  border-bottom:1px solid #161d26;font-size:13.5px}
.toggle:last-of-type{border-bottom:0}
.sw{width:44px;height:24px;background:#141a22;border:1px solid #2a3441;border-radius:999px;
  position:relative;cursor:pointer;transition:.15s;flex:0 0 auto}
.sw.on{background:var(--cyan);border-color:var(--cyan)}
.sw i{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;
  background:#8b97a8;transition:.15s}
.sw.on i{background:#06202e}
.sw.on i{left:22px}
#wheel{width:132px;height:132px;border-radius:50%;position:relative;cursor:crosshair;
  background:conic-gradient(from 0deg,#f33,#ff3,#3f3,#3ff,#33f,#f3f,#f33);flex:0 0 auto}
#wheel:after{content:"";position:absolute;inset:26px;border-radius:50%;background:var(--panel)}
#wknob{position:absolute;width:14px;height:14px;border-radius:50%;background:#fff;
  border:2px solid #05070a;transform:translate(-50%,-50%);pointer-events:none;z-index:2}
.legend{display:grid;gap:7px;margin-top:8px;font-size:12.5px;color:var(--dim)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;
  vertical-align:middle;border:1px solid #000}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{background:#141a22;color:#cfd9e8;border:1px solid var(--line);border-radius:999px;
  padding:6px 12px;font-size:12px;cursor:pointer}
.chip.on{background:var(--cyan);color:#06202e;border-color:var(--cyan);font-weight:600}
</style></head><body>
<header><h1>Hermes Dot</h1><span id="dev" class="pill">connecting…</span><span id="mode" class="pill">—</span></header>
<main>
  <section>
    <h2>Now playing</h2>
    <div id="song" class="song">Nothing playing</div>
    <div id="hint" class="dim">Press the Dot's button and say “play …”</div>
    <input id="q" type="text" style="margin-top:14px;width:100%"
           placeholder="Song, artist or anything…">
    <div class="row" style="margin-top:10px">
      <button onclick="play()" style="flex:1">Play</button>
      <button class="ghost" onclick="stop()" style="flex:1">Stop</button>
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
    <div class="dim" style="margin-top:14px">what the ring means</div>
    <div class="legend">
      <span><i style="background:#37d5ff"></i>listening — talk now</span>
      <span><i style="background:#0a5cff"></i>thinking</span>
      <span><i style="background:#41e08f"></i>speaking — press to interrupt</span>
      <span><i style="background:#20252e"></i>idle</span>
    </div>
  </section>
  <section>
    <h2>Lighting</h2>
    <div class="toggle"><span>Ring visualiser<br><span class="dim">the LEDs follow the music</span></span>
      <div id="vison" class="sw" onclick="toggleVis()"><i></i></div></div>
    <div class="row" style="margin:14px 0 6px;gap:16px;align-items:center">
      <div id="wheel" title="drag to set hue"></div>
      <div style="flex:1">
        <div class="dim" id="huetxt">hue 200°</div>
        <input id="bright" type="range" min="10" max="100" value="100" oninput="setBright(this.value)">
        <div class="dim" style="margin-top:10px">presets</div>
        <div id="presets" class="chips"></div>
      </div>
    </div>
  </section>
  <section>
    <h2>Recently played</h2>
    <ul id="hist"></ul>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
async function api(path,body){
  const T=new URLSearchParams(location.search).get('t');
  const u=T?(path+(path.includes('?')?'&':'?')+'t='+encodeURIComponent(T)):path;
  const r=await fetch(u,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function play(){const q=$('#q').value.trim(); if(q) api('/api/play',{q}); }
function stop(){api('/api/stop',{});}
function setVol(v){$('#volv').textContent=v; api('/api/volume',{level:+v});}
function eq(){for(const b of ['bass','mid','treble']){$('#'+b+'v').textContent=$('#'+b).value+' dB';}
  api('/api/eq',{bass:+$('#bass').value,mid:+$('#mid').value,treble:+$('#treble').value});}
let LIGHT={hue:200,preset:'equalizer',enabled:true,brightness:1};
function visPost(patch){
  LIGHT=Object.assign({},LIGHT,patch);
  api('/api/lighting',patch);
}
function toggleVis(){ visPost({enabled:!LIGHT.enabled}); paintLight(); }
function setBright(v){ visPost({brightness:v/100}); }
function choosePreset(name){ visPost({preset:name}); paintLight(); }
function paintLight(){
  document.querySelector('#vison').className='sw'+(LIGHT.enabled?' on':'');
  document.querySelector('#bright').value=Math.round((LIGHT.brightness||1)*100);
  document.querySelector('#huetxt').textContent='hue '+LIGHT.hue+'°';
  document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',c.dataset.p===LIGHT.preset));
  const w=document.querySelector('#wheel'), r=w.getBoundingClientRect();
  const a=(LIGHT.hue-90)*Math.PI/180, rad=r.width/2-9;
  const k=document.querySelector('#wknob');
  k.style.left=(r.width/2+Math.cos(a)*rad)+'px';
  k.style.top=(r.height/2+Math.sin(a)*rad)+'px';
}
function initWheel(){
  const w=document.querySelector('#wheel');
  const knob=document.createElement('div'); knob.id='wknob'; w.appendChild(knob);
  const set=e=>{
    const r=w.getBoundingClientRect();
    const dx=e.clientX-r.left-r.width/2, dy=e.clientY-r.top-r.height/2;
    let deg=Math.atan2(dy,dx)*180/Math.PI+90; if(deg<0) deg+=360;
    visPost({hue:Math.round(deg)}); paintLight();
  };
  let down=false;
  w.addEventListener('pointerdown',e=>{down=true;w.setPointerCapture(e.pointerId);set(e);});
  w.addEventListener('pointermove',e=>{if(down)set(e);});
  w.addEventListener('pointerup',()=>{down=false;});
  ['equalizer','ocean','sunset','forest','mono','rainbow','pulse'].forEach(n=>{
    const b=document.createElement('button'); b.className='chip'; b.textContent=n;
    b.dataset.p=n; b.onclick=()=>choosePreset(n);
    document.querySelector('#presets').appendChild(b);
  });
}
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
  if(s.lighting){ LIGHT=Object.assign({},LIGHT,s.lighting); paintLight(); }
  $('#hist').innerHTML=(s.history||[]).map(h=>`<li onclick="api('/api/play',{q:'${(h.query||'').replace(/'/g,'')}'})"><span>${h.title}</span><span class="dim">${new Date(h.at*1000).toLocaleTimeString()}</span></li>`).join('')||'<li class="dim">Nothing yet</li>';
}
initWheel(); refresh(); setInterval(refresh,4000);
</script></body></html>"""
