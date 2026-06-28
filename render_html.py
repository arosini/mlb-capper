"""HTML page renderer — _html_game(), render_html_page(), CSS, and JS."""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from teams import _LOGO, logo_img
from analysis import flt, xera_label
from odds import fmt_k_line, fmt_outs_line
from suggestions import _pick_dom_id, _pick_summary_title, _render_suggestions_html, _ai_game_map

_ET = timezone(timedelta(hours=-4))


def _h(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:15px;line-height:1.5;background:#f3f4f6;color:#111827;padding-bottom:2rem}
header{background:#0f172a;color:white;padding:.875rem 1rem;text-align:center;position:sticky;top:0;z-index:10}
header h1{font-size:1.15rem;font-weight:700;letter-spacing:-.01em}
.sub{font-size:.73rem;color:#94a3b8;margin-top:.2rem}
main{max-width:580px;margin:0 auto;padding:.5rem .625rem}
.game{background:white;margin-bottom:.5rem;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden}
.game>summary{list-style:none;cursor:pointer;padding:.7rem .875rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem;-webkit-tap-highlight-color:transparent;user-select:none}
.game>summary::-webkit-details-marker{display:none}
.game[open]>summary{border-bottom:1px solid #f0f0f0}
.gs-matchup{flex:1;min-width:0}
.gs-teams{font-size:.975rem;font-weight:700;display:flex;align-items:center;gap:.3rem}
.gs-venue{font-size:.7rem;color:#9ca3af;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm-logo{width:22px;height:22px;object-fit:contain;flex-shrink:0}
.tm-logo-sm{width:14px;height:14px;object-fit:contain;vertical-align:middle}
.gd{padding:.7rem .875rem .875rem;display:flex;flex-direction:column;gap:.7rem}
.sec-hd{font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;margin-bottom:.28rem}
.hb{background:#e5e7eb;color:#374151;font-size:.63rem;font-weight:700;padding:.04rem .26rem;border-radius:3px}
.xr{font-weight:600}
.era-elite{color:#16a34a}.era-good{color:#2563eb}.era-avg{color:#6b7280}.era-below{color:#d97706}.era-poor{color:#dc2626}.era-na{color:#9ca3af}
.wrc-elite{color:#16a34a}.wrc-above{color:#2563eb}.wrc-avg{color:#6b7280}.wrc-below{color:#d97706}.wrc-poor{color:#dc2626}
.dim{color:#9ca3af;font-size:.795rem}
.mu-outer{display:grid;grid-template-columns:1fr 1px 1fr;gap:0 .55rem;align-items:start}
.mu-col{display:flex;flex-direction:column;gap:.4rem;min-width:0}
.mu-divider{background:rgba(0,0,0,.1);align-self:stretch}
.sec{border:1px solid rgba(0,0,0,.09);border-radius:.42rem;overflow:hidden}
.sec-sum{display:flex;align-items:center;padding:.38rem .55rem;cursor:pointer;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;list-style:none;user-select:none}
.sec-sum::-webkit-details-marker{display:none}
.sec-sum::after{content:'▾';margin-left:auto;font-size:.6rem;opacity:.7}
.sec:not([open])>.sec-sum::after{content:'▸'}
.sec-body{padding:.3rem .5rem .5rem}
.mu-card{background:rgba(0,0,0,.028);border-radius:.35rem;padding:.35rem .5rem}
.mu-card-hd{font-size:.75rem;font-weight:700;margin-bottom:.25rem}
.mu-2c{display:grid;grid-template-columns:auto 1fr;gap:.13rem .5rem;font-size:.82rem;align-items:baseline}
.mu-lbl{color:#9ca3af;font-size:.75rem;white-space:nowrap}
.mu-v{font-weight:600;font-variant-numeric:tabular-nums}
.ot-wrap{font-size:.74rem}
.ot-row{display:grid;grid-template-columns:3rem 3.2rem 2rem 2.4rem 2.2rem 1.5rem 1.5rem 1.5rem 1.5rem 1.5rem;gap:.06rem .18rem;align-items:center;padding:.04rem 0}
.ot-hd span{font-size:.62rem;font-weight:700;color:#9ca3af;text-align:center}
.ot-hd span:first-child{text-align:left}
.ot-row span{text-align:center}
.ot-row span:first-child{text-align:left}
.ot-w{color:#16a34a;font-weight:700}
.ot-l{color:#dc2626;font-weight:700}
.ot-nd{color:#9ca3af}
.bp-row{display:flex;align-items:flex-start;gap:.4rem;font-size:.845rem;padding:.18rem 0}
.tm{font-weight:700;font-size:.77rem;min-width:2.3rem;padding-top:.1rem}
.bp-body{flex:1;min-width:0}
.stats{display:flex;flex-wrap:wrap;gap:.15rem .5rem;font-size:.8rem;color:#6b7280}
.stats b{color:#374151;font-weight:600}
.odds-grid{display:grid;grid-template-columns:2.4rem 1fr 1fr 1fr;gap:.18rem .4rem;font-size:.82rem;align-items:center}
.odds-hd{font-size:.6rem;font-weight:700;color:#9ca3af;text-align:center;text-transform:uppercase;letter-spacing:.04em}
.odds-val{text-align:center;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.odds-sub{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;margin-top:.45rem;margin-bottom:.1rem}
.odds-prop-row{display:flex;align-items:center;gap:.4rem;font-size:.82rem;margin:.15rem 0}
.odds-prop-lbl{font-size:.6rem;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.section-hd{font-size:.85rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;border-top:1px solid #e5e7eb;margin:1.2rem 0 .5rem;padding-top:.9rem}
@media(prefers-color-scheme:dark){.section-hd{color:#9ca3af;border-top-color:#374151}}
.flags{list-style:none}
.flags li{font-size:.78rem;color:#92400e;background:#fffbeb;border-left:3px solid #f59e0b;padding:.18rem .45rem;margin-top:.2rem;border-radius:0 4px 4px 0}
.trends{list-style:none;display:flex;flex-direction:column;gap:.15rem}
.trends li{font-size:.79rem;padding:.12rem 0}
.trend-hd{font-size:.7rem;font-weight:700;color:#374151;padding:.3rem 0 .05rem;border-top:1px solid rgba(0,0,0,.07);margin-top:.2rem}
.trend-hd:first-child{border-top:none;margin-top:0;padding-top:0}
.tw{color:#16a34a;font-weight:700}.tl{color:#dc2626;font-weight:700}
.wx-badge{font-size:.63rem;font-weight:700;background:#e0f2fe;color:#0369a1;padding:.05rem .35rem;border-radius:3px;white-space:nowrap;margin-left:.4rem}
.wx-badge.wx-warn{background:#fef3c7;color:#92400e}
.wx-badge.wx-hot{background:#fee2e2;color:#b91c1c}
.wx-badge.wx-hitter{background:#fef3c7;color:#92400e}
.wx-badge.wx-pitcher{background:#d1fae5;color:#065f46}
@media(prefers-color-scheme:dark){
body{background:#0f0f0f;color:#e5e5e5}
header{background:#030712}
.game{background:#1a1a1a;border-color:#2a2a2a}
.game[open]>summary{border-bottom-color:#2a2a2a}
.gs-venue{color:#6b7280}
.sec-hd{color:#6b7280}
.mu-card{background:rgba(255,255,255,.05)}
.mu-divider{background:rgba(255,255,255,.12)}
.sec{border-color:#2a2a2a}
.mu-lbl{color:#6b7280}
.ot-hd span{color:#6b7280}
.stats b{color:#d1d5db}
.hb{background:#374151;color:#d1d5db}
.flags li{background:#1c1400;border-left-color:#b45309;color:#fbbf24}
.trend-hd{color:#d1d5db;border-top-color:rgba(255,255,255,.1)}
.wx-badge{background:#0c2a3a;color:#7dd3fc}
.wx-badge.wx-warn{background:#2d1a00;color:#fbbf24}
.wx-badge.wx-hot{background:#2d0a0a;color:#fca5a5}
.wx-badge.wx-hitter{background:#2d1a00;color:#fbbf24}
.wx-badge.wx-pitcher{background:#022c22;color:#6ee7b7}
}
.spl-row{display:grid;grid-template-columns:6rem 2.4rem 2.8rem 1.8rem 1.8rem 1.8rem 1.5rem;gap:.05rem .3rem;align-items:center;padding:.15rem 0;font-size:.79rem}
.spl-hd span{font-size:.6rem;font-weight:700;color:#9ca3af;text-align:center;text-transform:uppercase;letter-spacing:.03em}
.spl-hd span:first-child{text-align:left}
.spl-ctx{font-weight:600;font-size:.75rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spl-val{text-align:center;font-variant-numeric:tabular-nums;font-weight:600}
.spl-n{text-align:center;color:#9ca3af;font-size:.65rem}
.spl-sp-hd{font-size:.72rem;font-weight:700;color:#374151;padding:.32rem 0 .08rem;border-top:1px solid rgba(0,0,0,.07)}
.spl-sp-hd:first-child{border-top:none;padding-top:0}
@media(prefers-color-scheme:dark){
.spl-hd span{color:#6b7280}
.spl-sp-hd{color:#d1d5db;border-top-color:rgba(255,255,255,.1)}
}
.ai-picks{background:white;margin:.5rem 0 .75rem;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden}
.ai-picks-hd{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#9ca3af;padding:.45rem .875rem .3rem;cursor:pointer;list-style:none}
.ai-picks[open] .ai-picks-hd{border-bottom:1px solid #f0f0f0}
.ai-game{font-size:.76rem;font-weight:700;color:#374151}
.ai-bet{font-size:.95rem;font-weight:700;margin:.12rem 0}
.ai-odds{font-size:.76rem;color:#6b7280;font-variant-numeric:tabular-nums}
.ai-reason{font-size:.76rem;color:#374151;margin-top:.28rem;line-height:1.45}
.ai-conf{font-size:.54rem;background:#fde68a;color:#92400e;padding:.04rem .26rem;border-radius:3px;font-weight:700;vertical-align:middle;margin-left:.3rem;text-transform:uppercase;letter-spacing:.04em}
.ai-line-warn{font-size:.71rem;color:#b45309;background:#fff7ed;border-left:3px solid #f97316;padding:.15rem .42rem;margin-top:.28rem;border-radius:0 4px 4px 0}
.ai-no-best{font-size:.77rem;color:#6b7280;padding:.5rem .875rem;font-style:italic}
.ai-others-wrap{padding:.45rem .875rem .5rem}
.ai-others-label{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;margin-bottom:.28rem}
.ai-other{border:1px solid #e5e7eb;border-radius:7px;padding:.38rem .55rem;margin-bottom:.32rem}
.ai-other:last-child{margin-bottom:0}
.ai-disclaimer{font-size:.61rem;color:#9ca3af;text-align:center;padding:.3rem .875rem .45rem;border-top:1px solid #f0f0f0;margin-top:.1rem}
.ai-check{display:inline-flex;align-items:center;justify-content:center;width:1.1rem;height:1.1rem;background:#16a34a;color:#fff;border-radius:50%;font-size:.62rem;font-weight:800;margin-left:.4rem;vertical-align:middle;flex-shrink:0;line-height:1}
.ai-pick-card .sec-sum{color:#15803d}
.ai-pick-inline{font-size:.78rem;padding:.05rem 0}
.ai-pick-inline .ai-bet{font-size:.88rem;font-weight:700;margin:.1rem 0}
.ai-pick-inline .ai-odds{font-size:.74rem;color:#6b7280}
.ai-pick-inline .ai-reason{font-size:.73rem;color:#374151;margin-top:.2rem;line-height:1.45}
.ai-pass-reason{font-size:.76rem;color:#6b7280;font-style:italic;padding:.1rem 0}
.ai-found-at{font-size:.65rem;color:#9ca3af;margin-top:.15rem}
.ai-active-wrap{padding:.55rem .875rem .5rem}
.ai-started-wrap{padding:.45rem .875rem .5rem;border-top:1px solid #f0f0f0}
.ai-started-label{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;margin-bottom:.28rem}
.ai-conf-dim{font-size:.54rem;background:#f3f4f6;color:#6b7280;padding:.04rem .26rem;border-radius:3px;font-weight:700;vertical-align:middle;margin-left:.3rem;text-transform:uppercase;letter-spacing:.04em}
.ai-pick-row{border:1px solid #e5e7eb;border-radius:7px;margin-bottom:.32rem;overflow:hidden}
.ai-pick-row:last-child{margin-bottom:0}
.ai-pick-sum{display:flex;align-items:center;padding:.38rem .55rem;cursor:pointer;font-size:.8rem;font-weight:600;color:#374151;list-style:none;user-select:none;gap:.4rem}
.ai-pick-sum::-webkit-details-marker{display:none}
.ai-pick-sum::after{content:'▸';margin-left:auto;font-size:.6rem;opacity:.7;flex-shrink:0}
.ai-pick-row[open]>.ai-pick-sum::after{content:'▾'}
.ai-pick-body{padding:.3rem .55rem .45rem;border-top:1px solid #f0f0f0}
@media(prefers-color-scheme:dark){
.ai-picks{background:#1a1a1a;border-color:#2a2a2a}
.ai-picks[open] .ai-picks-hd{border-bottom-color:#2a2a2a}
.ai-game{color:#d1d5db}
.ai-reason{color:#d1d5db}
.ai-conf{background:#92400e;color:#fde68a}
.ai-line-warn{background:#2a1500;color:#fbbf24;border-left-color:#f97316}
.ai-no-best{color:#9ca3af}
.ai-other{border-color:#2a2a2a}
.ai-others-wrap .ai-game{color:#d1d5db}
.ai-others-wrap .ai-reason{color:#9ca3af}
.ai-disclaimer{border-top-color:#2a2a2a;color:#6b7280}
.ai-pick-card .sec-sum{color:#4ade80}
.ai-pick-inline .ai-reason{color:#d1d5db}
.ai-pass-reason{color:#9ca3af}
.ai-found-at{color:#4b5563}
.ai-started-wrap{border-top-color:#2a2a2a}
.ai-started-label{color:#4b5563}
.ai-conf-dim{background:#2a2a2a;color:#9ca3af}
.ai-pick-row{border-color:#2a2a2a}
.ai-pick-sum{color:#d1d5db}
.ai-pick-body{border-top-color:#2a2a2a}
}
"""

# ── CSS class helpers ─────────────────────────────────────────────────────────

def _era_cls(label: str) -> str:
    return {"elite": "era-elite", "good": "era-good", "avg": "era-avg",
            "below avg": "era-below", "poor": "era-poor"}.get(label, "era-na")


def _wrc_cls(label: str) -> str:
    return {"elite": "wrc-elite", "above avg": "wrc-above", "avg": "wrc-avg",
            "below avg": "wrc-below", "poor": "wrc-poor"}.get(label, "")


def _k_sp_cls(v):
    if v is None: return "era-na"
    if v >= 28: return "era-elite"
    if v >= 23: return "era-good"
    if v >= 17: return "era-avg"
    if v >= 12: return "era-below"
    return "era-poor"


def _k_sp_lbl(v):
    if v is None: return ""
    if v >= 28: return "elite"
    if v >= 23: return "good"
    if v >= 17: return "avg"
    if v >= 12: return "below avg"
    return "poor"


def _k_bat_cls(v):
    """High lineup K% = more strikeouts = bad for offense."""
    if v is None: return ""
    if v >= 28: return "wrc-poor"
    if v >= 24: return "wrc-below"
    if v >= 20: return "wrc-avg"
    if v >= 16: return "wrc-above"
    return "wrc-elite"


def _k_bat_lbl(v):
    if v is None: return ""
    if v >= 28: return "poor"
    if v >= 24: return "below avg"
    if v >= 20: return "avg"
    if v >= 16: return "above avg"
    return "elite"


def _hh_sp_cls(v):
    """Low HH% allowed = good for pitcher."""
    if v is None: return "era-na"
    if v <= 30: return "era-elite"
    if v <= 35: return "era-good"
    if v <= 40: return "era-avg"
    if v <= 45: return "era-below"
    return "era-poor"


def _hh_sp_lbl(v):
    if v is None: return ""
    if v <= 30: return "elite"
    if v <= 35: return "good"
    if v <= 40: return "avg"
    if v <= 45: return "below avg"
    return "poor"


def _hh_bat_cls(v):
    """High HH% = good for offense (they hit the ball hard)."""
    if v is None: return ""
    if v >= 45: return "wrc-elite"
    if v >= 40: return "wrc-above"
    if v >= 35: return "wrc-avg"
    if v >= 30: return "wrc-below"
    return "wrc-poor"


def _hh_bat_lbl(v):
    if v is None: return ""
    if v >= 45: return "elite"
    if v >= 40: return "above avg"
    if v >= 35: return "avg"
    if v >= 30: return "below avg"
    return "poor"


def _barrel_sp_cls(v):
    """Low Barrel% allowed = good for pitcher."""
    if v is None: return "era-na"
    if v <= 5:  return "era-elite"
    if v <= 8:  return "era-good"
    if v <= 11: return "era-avg"
    if v <= 15: return "era-below"
    return "era-poor"


def _barrel_sp_lbl(v):
    if v is None: return ""
    if v <= 5:  return "elite"
    if v <= 8:  return "good"
    if v <= 11: return "avg"
    if v <= 15: return "below avg"
    return "poor"


def _apf_cls_lbl(v):
    if v is None: return "era-avg", "Neutral"
    if v >= 108: return "era-poor",  "Hitter Friendly"
    if v >= 103: return "era-below", "Hitter Friendly"
    if v >= 97:  return "era-avg",   "Neutral"
    if v >= 93:  return "era-good",  "Pitcher Friendly"
    return "era-elite", "Pitcher Friendly"


def _wx_summary(wx: dict) -> tuple:
    """Return (label, css_class) for weather badge. Empty label = no badge."""
    if not wx:
        return "", ""
    desc = (wx.get("weather_description") or "").lower()
    if any(x in desc for x in ("thunder", "lightning", "storm")):
        return "Lightning", "wx-warn"
    parts = []
    cls = ""
    if wx.get("precip_risk_during_game") or any(x in desc for x in ("rain", "drizzle", "shower")):
        parts.append("Rainy")
    temp = wx.get("temperature")
    if temp is not None:
        if temp < 50:
            parts.append("Cold")
        elif temp > 90:
            parts.append("Hot")
            cls = "wx-hot"
    wind = wx.get("wind_speed")
    if wind is not None and wind > 15:
        parts.append("Windy")
    return ", ".join(parts), cls or ("wx-warn" if parts else "")


# ── Time sorting ──────────────────────────────────────────────────────────────

def _time_sort_key(g: dict) -> int:
    # MLB schedule game_date is authoritative; wx.game_time_local is fallback only
    gd = g.get("game_date", "")
    if gd:
        try:
            dt_utc = datetime.fromisoformat(gd.replace("Z", "+00:00"))
            dt_et = dt_utc.astimezone(_ET)
            return dt_et.hour * 60 + dt_et.minute
        except Exception:
            pass
    t = (g.get("wx") or {}).get("game_time_local", "")
    m = re.match(r'(\d+):(\d+)\s*(AM|PM)', t)
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and h != 12: h += 12
        elif ampm == "AM" and h == 12: h = 0
        return h * 60 + mn
    return 9999


# ── JavaScript ────────────────────────────────────────────────────────────────

_SPLIT_SCRIPT = """
<script>
(function(){
  var GAME_STORE='mlb_open';
  var SEC_STORE='mlb_sec_closed';
  var PICKS_STORE='mlb_picks_open';
  function etMin(){
    var et=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
    return et.getHours()*60+et.getMinutes();
  }
  function split(){
    var now=etMin();
    var main=document.querySelector('main');
    if(!main)return;
    var cards=Array.from(main.querySelectorAll('details.game[data-start-min]'));
    var started=cards.filter(function(c){return +c.dataset.startMin<=now&&+c.dataset.startMin<1440;});
    if(!started.length)return;
    var hd=document.createElement('h2');
    hd.className='section-hd';
    hd.textContent='In Progress / Completed';
    main.appendChild(hd);
    started.forEach(function(c){main.appendChild(c);});
  }
  function saveGames(){
    var open=Array.from(document.querySelectorAll('details.game[open]')).map(function(d){return d.id;});
    try{localStorage.setItem(GAME_STORE,JSON.stringify(open));}catch(e){}
  }
  function restoreGames(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(GAME_STORE)||'[]');}catch(e){saved=[];}
    if(!saved.length)return;
    var ids=new Set(saved);
    document.querySelectorAll('details.game').forEach(function(d){
      if(ids.has(d.id))d.setAttribute('open','');
    });
  }
  function saveSections(){
    var state={};
    document.querySelectorAll('details.sec').forEach(function(d){
      if(d.id)state[d.id]=d.hasAttribute('open');
    });
    try{localStorage.setItem(SEC_STORE,JSON.stringify(state));}catch(e){}
  }
  function restoreSections(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(SEC_STORE)||'null');}catch(e){saved=null;}
    if(!saved)return;
    document.querySelectorAll('details.sec').forEach(function(d){
      if(d.id in saved){
        if(saved[d.id])d.setAttribute('open','');
        else d.removeAttribute('open');
      }
    });
  }
  function savePicksState(){
    var state={};
    var card=document.getElementById('ai-picks-card');
    if(card)state['ai-picks-card']=card.hasAttribute('open');
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      state[d.id]=d.hasAttribute('open');
    });
    try{localStorage.setItem(PICKS_STORE,JSON.stringify(state));}catch(e){}
  }
  function restorePicksState(){
    var saved;
    try{saved=JSON.parse(localStorage.getItem(PICKS_STORE)||'null');}catch(e){saved=null;}
    if(!saved)return;
    var card=document.getElementById('ai-picks-card');
    if(card&&'ai-picks-card' in saved){
      if(saved['ai-picks-card'])card.setAttribute('open','');
      else card.removeAttribute('open');
    }
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      if(d.id in saved){
        if(saved[d.id])d.setAttribute('open','');
        else d.removeAttribute('open');
      }
    });
  }
  function localTs(){
    document.querySelectorAll('.local-ts[data-utc]').forEach(function(el){
      try{
        var d=new Date(el.dataset.utc);
        el.textContent=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit',timeZoneName:'short'});
      }catch(e){}
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    split();
    restoreGames();
    restoreSections();
    restorePicksState();
    localTs();
    document.querySelectorAll('details.game').forEach(function(d){
      d.addEventListener('toggle',saveGames);
    });
    document.querySelectorAll('details.sec').forEach(function(d){
      d.addEventListener('toggle',saveSections);
    });
    var card=document.getElementById('ai-picks-card');
    if(card)card.addEventListener('toggle',savePicksState);
    document.querySelectorAll('details.ai-pick-row[id]').forEach(function(d){
      d.addEventListener('toggle',savePicksState);
    });
  });
})();
</script>"""


def _ts_span(iso: str) -> str:
    """Render a UTC ISO timestamp as a span the JS will localize to browser TZ."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        fallback = dt.strftime("%H:%M UTC")
    except Exception:
        fallback = iso
    return f'<span class="local-ts" data-utc="{_h(iso)}">{_h(fallback)}</span>'


# ── Per-game card ─────────────────────────────────────────────────────────────

def _html_game(g: dict, ai_pick: Optional[dict] = None) -> str:
    away, home = g["away"], g["home"]
    sp_a, sp_h = g["away_sp"], g["home_sp"]
    of_a, of_h = g["away_off"], g["home_off"]
    bp_a, bp_h = g["away_bp"], g["home_bp"]

    wx = g["wx"] or {}
    roof = wx.get("roof_status", "")
    if not roof or roof in ("Open Air", "N/A") or "open" in roof.lower():
        indoor_label = None
    elif "dome" in roof.lower():
        indoor_label = "Dome"
    elif "closed" in roof.lower():
        indoor_label = "Roof Closed"
    else:
        indoor_label = roof
    is_open_air = indoor_label is None
    roof_paren = f" ({indoor_label})" if indoor_label else ""
    venue_str = (g["venue"] or "") + roof_paren

    time_str = ""
    if g.get("game_date"):
        try:
            _dt = datetime.fromisoformat(g["game_date"].replace("Z", "+00:00")).astimezone(_ET)
            _h12 = _dt.hour % 12 or 12
            time_str = f"{_h12}:{_dt.minute:02d} {'PM' if _dt.hour >= 12 else 'AM'}"
        except Exception:
            pass
    if not time_str:
        time_str = wx.get("game_time_local", "").replace(" ET", "").strip()
    venue_parts = [p for p in [time_str, venue_str] if p.strip()]
    wx_lbl, wx_cls = _wx_summary(wx)
    apf_raw = wx.get("adjusted_park_factor") if wx else None
    _apf_cls_pre, apf_lbl_pre = _apf_cls_lbl(apf_raw)
    apf_display = "Neutral Conditions" if apf_lbl_pre == "Neutral" else apf_lbl_pre

    if is_open_air:
        if wx_lbl:
            effective_wx_lbl = wx_lbl
            effective_wx_cls = wx_cls
        elif apf_raw is not None:
            effective_wx_lbl = apf_display
            if "Hitter" in apf_lbl_pre:
                effective_wx_cls = "wx-hitter"
            elif "Pitcher" in apf_lbl_pre:
                effective_wx_cls = "wx-pitcher"
            else:
                effective_wx_cls = ""
        else:
            effective_wx_lbl = ""
            effective_wx_cls = ""
    else:
        effective_wx_lbl = ""
        effective_wx_cls = ""

    wx_badge_html = (f'<span class="wx-badge {effective_wx_cls}">{_h(effective_wx_lbl)}</span>'
                     if effective_wx_lbl else "")
    venue_html = (f'<span class="gs-venue">{_h("  ·  ".join(venue_parts))}{wx_badge_html}</span>'
                  if venue_parts else "")

    def _row(lbl, val_s, cls="", lbl_txt=""):
        if val_s == "?":
            return f'<span class="mu-lbl">{_h(lbl)}</span><span class="dim">?</span>'
        lbl_part = f' <span class="dim">({_h(lbl_txt)})</span>' if lbl_txt else ""
        cls_attr = f' class="mu-v {cls}"' if cls else ' class="mu-v"'
        return f'<span class="mu-lbl">{_h(lbl)}</span><span{cls_attr}>{_h(val_s)}{lbl_part}</span>'

    def _outing_avg(outings, key, n=3):
        vals = [o[key] for o in outings[:n] if o.get(key) is not None]
        return f"{sum(vals)/len(vals):.0f}" if vals else None

    def _sp_card(sp, pc_avg=None):
        ec = _era_cls(sp["label"])
        rows  = _row("xERA",    sp["xera_s"], ec,             sp["label"])
        k_v   = flt(sp["k"])
        rows += _row("K%",      sp["k"],      _k_sp_cls(k_v),  _k_sp_lbl(k_v))
        hh_v  = flt(sp["hard"])
        rows += _row("HH%",     sp["hard"],   _hh_sp_cls(hh_v), _hh_sp_lbl(hh_v))
        bv = flt(sp["barrel"])
        rows += _row("Barrel%", sp["barrel"], _barrel_sp_cls(bv), _barrel_sp_lbl(bv))
        rows += _row("ERA",     sp["era_s"])
        rows += f'<span class="mu-lbl">IP/gs</span><span class="dim">{_h(sp["depth"])}</span>'
        rows += f'<span class="mu-lbl">H/gs</span><span class="dim">{_h(sp["h_per_gs"])}</span>'
        pc_display = pc_avg if (pc_avg and sp.get("has_stats")) else "?"
        rows += f'<span class="mu-lbl">PC/gs</span><span class="dim">{_h(pc_display)}</span>'
        rows += f'<span class="mu-lbl">BB%</span><span class="dim">{_h(sp["bb"])}</span>'
        hb = f'<span class="hb">{_h(sp["hand"])}</span>' if sp["hand"] != "?" else ""
        return (f'<div class="mu-card"><div class="mu-card-hd">{_h(sp["name"])} {hb}</div>'
                f'<div class="mu-2c">{rows}</div></div>')

    def _bat_card(team, off):
        if off:
            wc = _wrc_cls(off["label"])
            rows  = _row("wRC+", off["wrc_s"], wc, off["label"])
            k_v   = flt(off["k"])
            rows += _row("K%",  off["k"],   _k_bat_cls(k_v),  _k_bat_lbl(k_v))
            hh_v  = flt(off["hard"])
            rows += _row("HH%", off["hard"], _hh_bat_cls(hh_v), _hh_bat_lbl(hh_v))
            vs = f'vs {off["vs_hand"]}'
        else:
            rows = f'<span class="dim" style="grid-column:1/-1;font-size:.8rem">No data</span>'
            vs = ""
        return (f'<div class="mu-card"><div class="mu-card-hd">'
                f'{_h(team)} <span class="dim" style="font-weight:400">{_h(vs)}</span></div>'
                f'<div class="mu-2c">{rows}</div></div>')

    def _outing_table(outings):
        if not outings:
            return ""
        def _v(v): return "—" if v is None else str(v)
        hdr = ('<div class="ot-row ot-hd">'
               '<span>Date</span><span>Opp</span><span>Res</span>'
               '<span>IP</span><span>PC</span><span>K</span><span>H</span><span>BB</span><span>ER</span><span>R</span>'
               '</div>')
        rows = ""
        for o in outings:
            rc  = "ot-w" if o["result"] == "W" else "ot-l" if o["result"] == "L" else "ot-nd"
            pfx = "@" if o["ha"] == "@" else "vs"
            opp_code = o["opp"]
            opp_slug = _LOGO.get(opp_code, opp_code.lower())
            opp_url  = f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/mlb/500/{opp_slug}.png&h=28&w=28"
            opp_logo = f'<img src="{opp_url}" class="tm-logo-sm" alt="{_h(opp_code)}" onerror="this.style.display=\'none\'">'
            ip_s = (_v(o["ip"]) + "*") if o.get("is_relief") else _v(o["ip"])
            rows += (f'<div class="ot-row">'
                     f'<span class="dim">{_h(o["date"])}</span>'
                     f'<span class="dim">{pfx} {opp_logo}</span>'
                     f'<span class="{rc}">{_h(o["result"])}</span>'
                     f'<span>{_h(ip_s)}</span>'
                     f'<span class="dim">{_h(_v(o["pc"]))}</span>'
                     f'<span>{_h(_v(o["k"]))}</span>'
                     f'<span class="dim">{_h(_v(o["h"]))}</span>'
                     f'<span class="dim">{_h(_v(o["bb"]))}</span>'
                     f'<span>{_h(_v(o["er"]))}</span>'
                     f'<span class="dim">{_h(_v(o["r"]))}</span>'
                     f'</div>')
        return f'<div class="ot-wrap">{hdr}{rows}</div>'

    def _bp_row(team, bp):
        ec = _era_cls(bp["label"])
        lbl = f' <span class="dim">({_h(bp["label"])})</span>' if bp["label"] else ""
        stress_html = ""
        if bp.get("stress_label") and bp["stress_label"] != "No recent games":
            sc = bp["stress_css"]
            ip = bp.get("stress_ip")
            games = bp.get("stress_games", 0)
            ip_s = f"{ip:.1f} IP" if ip is not None else ""
            games_s = f"/{games}g" if games > 0 else ""
            stress_html = (
                f'<span class="{sc}"><b>2d stress</b> {_h(bp["stress_label"])}'
                f'<span class="dim"> ({ip_s}{games_s})</span></span>'
            )
        return (f'<div class="bp-row">'
                f'<span class="tm">{_h(team)}</span>'
                f'<div class="bp-body stats">'
                f'<span class="xr {ec}"><b>xERA</b> {_h(bp["xera_s"])}{lbl}</span>'
                f'<span><b>ERA</b> {_h(bp["era_s"])}</span>'
                f'{stress_html}'
                f'</div></div>')

    g_id = f"{_h(away)}-{_h(home)}"

    wx_html = ""
    if indoor_label:
        wx_html = (
            f'<details class="sec" id="{g_id}-weather">'
            f'<summary class="sec-sum">Weather · {_h(indoor_label)}</summary>'
            f'<div class="sec-body"><span class="dim">{_h(indoor_label)}</span></div>'
            f'</details>'
        )
    elif wx:
        parts = []
        if wx.get("temperature") is not None:
            parts.append(f"{wx['temperature']:.0f}°F")
        if wx.get("weather_description"):
            parts.append(wx["weather_description"])
        if wx.get("wind_speed") is not None:
            wd = wx.get("wind_direction_label", "")
            parts.append(f"Wind {wx['wind_speed']:.0f} mph {wd}".strip())
        rain_html = ""
        if wx.get("precip_risk_during_game"):
            prob = wx.get("precip_probability")
            rain_s = f"Rain possible ({prob:.0f}%)" if prob is not None else "Rain possible"
            rain_html = f' · <span class="era-below">{_h(rain_s)}</span>'
        apf = wx.get("adjusted_park_factor")
        apf_html = ""
        if apf is not None:
            apf_cls, apf_lbl = _apf_cls_lbl(apf)
            apf_html = f'<span class="{apf_cls}">APF {apf:.0f} — {apf_lbl}</span>'
        cond_line = ""
        if apf_html or rain_html:
            cond_line = f'<div>{apf_html}{rain_html}</div>'
        wx_body = (f'<div class="dim">{_h(", ".join(parts))}</div>' if parts else "") + cond_line
        wx_sum_lbl = f"Weather · {effective_wx_lbl}" if effective_wx_lbl else "Weather"
        wx_html = (
            f'<details class="sec" id="{g_id}-weather">'
            f'<summary class="sec-sum">{_h(wx_sum_lbl)}</summary>'
            f'<div class="sec-body">{wx_body}</div>'
            f'</details>'
        )

    flags_html = ""
    if g["flags"]:
        n = len(g["flags"])
        items = "".join(f'<li>{_h(f)}</li>' for f in g["flags"])
        flags_html = (
            f'<details class="sec" id="{g_id}-flags">'
            f'<summary class="sec-sum">Flags · {n}</summary>'
            f'<div class="sec-body"><ul class="flags">{items}</ul></div>'
            f'</details>'
        )

    _sub = ' style="text-transform:none;font-weight:400;font-size:.62rem"'
    od = g.get("odds")
    odds_html = ""
    if od:
        def _odds_rows(away_ml, home_ml, away_sp, home_sp, ov, un):
            return (
                f'<span></span><span class="odds-hd">ML</span>'
                f'<span class="odds-hd">Spread</span><span class="odds-hd">Total</span>'
                f'<span class="tm">{_h(away)}</span><span class="odds-val">{_h(away_ml)}</span>'
                f'<span class="odds-val">{_h(away_sp)}</span><span class="odds-val">{_h(ov)}</span>'
                f'<span class="tm">{_h(home)}</span><span class="odds-val">{_h(home_ml)}</span>'
                f'<span class="odds-val">{_h(home_sp)}</span><span class="odds-val">{_h(un)}</span>'
            )
        f5_html = ""
        if od.get("has_f5"):
            f5_html = (
                f'<div class="odds-sub">First 5 Innings</div>'
                f'<div class="odds-grid">'
                + _odds_rows(od["away_f5_ml"], od["home_f5_ml"],
                             od["away_f5_spread"], od["home_f5_spread"],
                             od["f5_over"], od["f5_under"])
                + f'</div>'
            )
        tt_html = ""
        if od.get("has_tt") or od.get("has_f5tt"):
            def _tt_row(team_name, over_s, under_s, f5_over_s="", f5_under_s=""):
                has_f5 = bool(f5_over_s and f5_over_s != "—")
                f5_cells = (f'<span class="odds-val dim">{_h(f5_over_s)}</span>'
                            f'<span class="odds-val dim">{_h(f5_under_s)}</span>') if has_f5 else ""
                return (f'<span class="tm">{_h(team_name)}</span>'
                        f'<span class="odds-val">{_h(over_s)}</span>'
                        f'<span class="odds-val">{_h(under_s)}</span>'
                        + f5_cells)
            show_f5tt = od.get("has_f5tt")
            cols = "1fr 1fr 1fr 1fr 1fr" if show_f5tt else "1fr 1fr 1fr"
            f5_hdrs = ('<span class="odds-hd">F5 O</span>'
                       '<span class="odds-hd">F5 U</span>') if show_f5tt else ""
            tt_html = (
                f'<div class="odds-sub">Team Totals</div>'
                f'<div class="odds-grid" style="grid-template-columns:{cols}">'
                f'<span></span><span class="odds-hd">Over</span><span class="odds-hd">Under</span>{f5_hdrs}'
                + _tt_row(away, od["away_tt_over"], od["away_tt_under"],
                          od.get("away_f5tt_over",""), od.get("away_f5tt_under",""))
                + _tt_row(home, od["home_tt_over"], od["home_tt_under"],
                          od.get("home_f5tt_over",""), od.get("home_f5tt_under",""))
                + f'</div>'
            )
        props_html = ""
        away_k_s = fmt_k_line(od.get("away_k"))
        home_k_s = fmt_k_line(od.get("home_k"))
        away_outs_s = fmt_outs_line(od.get("away_outs"))
        home_outs_s = fmt_outs_line(od.get("home_outs"))
        if away_k_s or home_k_s or away_outs_s or home_outs_s:
            has_outs = away_outs_s or home_outs_s
            cols = "1fr 1fr 1fr" if has_outs else "1fr 1fr"
            def _prop_val(s): return _h(re.sub(r'^(?:K|Outs) O/U ', '', s)) if s else "—"
            def _prop_row(name, k_s, outs_s):
                outs_cell = f'<span class="odds-val">{_prop_val(outs_s)}</span>' if has_outs else ""
                return (f'<span class="tm">{_h(name)}</span>'
                        f'<span class="odds-val">{_prop_val(k_s)}</span>'
                        + outs_cell)
            outs_hd = '<span class="odds-hd">Outs O/U</span>' if has_outs else ""
            props_html = (
                f'<div class="odds-sub">Pitcher Props</div>'
                f'<div class="odds-grid" style="grid-template-columns:{cols}">'
                f'<span></span><span class="odds-hd">K O/U</span>{outs_hd}'
                + _prop_row(sp_a["name"], away_k_s, away_outs_s)
                + _prop_row(sp_h["name"], home_k_s, home_outs_s)
                + f'</div>'
            )
        odds_html = (
            f'<details class="sec" id="{g_id}-odds">'
            f'<summary class="sec-sum">Betting Odds <span class="dim"{_sub}>· best of DK / FanDuel / Fanatics</span></summary>'
            f'<div class="sec-body">'
            f'<div class="odds-sub">Full Game</div>'
            f'<div class="odds-grid">'
            + _odds_rows(od["away_ml"], od["home_ml"],
                         od["away_spread"], od["home_spread"],
                         od["over"], od["under"])
            + f'</div>{f5_html}{tt_html}{props_html}</div></details>'
        )

    away_outings = g.get("away_sp_outings", [])
    home_outings = g.get("home_sp_outings", [])
    away_pc = _outing_avg(away_outings, "pc")
    home_pc = _outing_avg(home_outings, "pc")

    matchup_html = (
        f'<details class="sec" id="{g_id}-matchup" open>'
        f'<summary class="sec-sum">Matchup · SP Last 3 / Team Last 12</summary>'
        f'<div class="sec-body">'
        f'<div class="mu-outer">'
        f'<div class="mu-col">{_sp_card(sp_a, pc_avg=away_pc)}{_bat_card(home, of_h)}</div>'
        f'<div class="mu-divider"></div>'
        f'<div class="mu-col">{_sp_card(sp_h, pc_avg=home_pc)}{_bat_card(away, of_a)}</div>'
        f'</div></div></details>'
    )

    outings_a = _outing_table(away_outings)
    outings_h = _outing_table(home_outings)
    outings_html = ""
    if outings_a:
        outings_html += (
            f'<details class="sec" id="{g_id}-outings-away">'
            f'<summary class="sec-sum">{_h(sp_a["name"])} · Last 5 Outings</summary>'
            f'<div class="sec-body">{outings_a}</div>'
            f'</details>'
        )
    if outings_h:
        outings_html += (
            f'<details class="sec" id="{g_id}-outings-home">'
            f'<summary class="sec-sum">{_h(sp_h["name"])} · Last 5 Outings</summary>'
            f'<div class="sec-body">{outings_h}</div>'
            f'</details>'
        )

    def _spl_row(ctx: str, stats: Optional[dict]) -> str:
        if not stats:
            return (f'<div class="spl-row">'
                    f'<span class="spl-ctx dim">{_h(ctx)}</span>'
                    f'<span class="dim" style="grid-column:2/-1">—</span>'
                    f'</div>')
        era_f = stats.get("era_f")
        ec = _era_cls(xera_label(era_f)) if era_f is not None else "era-na"
        return (
            f'<div class="spl-row">'
            f'<span class="spl-ctx">{_h(ctx)}</span>'
            f'<span class="spl-val">{_h(stats["ip"])}</span>'
            f'<span class="spl-val {ec}">{_h(stats["era"])}</span>'
            f'<span class="spl-val">{_h(stats["k"])}</span>'
            f'<span class="spl-val">{_h(stats["h"])}</span>'
            f'<span class="spl-val">{_h(stats["bb"])}</span>'
            f'<span class="spl-n">({stats["n"]})</span>'
            f'</div>'
        )

    def _spl_hdr() -> str:
        return (
            '<div class="spl-row spl-hd">'
            '<span></span><span>IP</span><span>ERA</span>'
            '<span>K</span><span>H</span><span>BB</span><span></span>'
            '</div>'
        )

    def _spl_block(sp_name: str, spl: dict, vs_lbl: str, at_lbl: str) -> str:
        if not spl.get("vs") and not spl.get("at"):
            return ""
        return (
            f'<div class="spl-sp-hd">{_h(sp_name)}</div>'
            + _spl_hdr()
            + _spl_row(vs_lbl, spl.get("vs"))
            + _spl_row(at_lbl, spl.get("at"))
        )

    away_spl = g.get("away_sp_splits", {})
    home_spl = g.get("home_sp_splits", {})
    splits_inner = (
        _spl_block(sp_a["name"], away_spl, f"vs {home}", f"at {home}")
        + _spl_block(sp_h["name"], home_spl, f"vs {away}", "home starts")
    )
    splits_html = (
        f'<details class="sec" id="{g_id}-splits">'
        f'<summary class="sec-sum">SP vs Opp / At Park · last 3 (2 seasons)</summary>'
        f'<div class="sec-body">{splits_inner}</div>'
        f'</details>'
    ) if splits_inner.strip() else ""

    def _trend_block(team: str, sp_name: str, tr: Optional[dict], is_away: bool) -> str:
        if not tr:
            return ""
        side_lbl = "home" if tr["is_home"] else "away"
        opp = home if is_away else away
        lines = []

        def _wl_s(w, l):
            return f'<span class="tw">{w}</span>-<span class="tl">{l}</span>'

        h2h = g.get("h2h", {})
        if h2h and h2h.get("total", 0) >= 2:
            my_w = h2h["away_wins"] if is_away else h2h["home_wins"]
            op_w = h2h["home_wins"] if is_away else h2h["away_wins"]
            n_h2h = h2h["total"]
            lines.append(f'{_h(team)} are {_wl_s(my_w, op_w)} vs {_h(opp)} this season ({n_h2h} games).')

        n10 = tr["n_last10"]
        if n10:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last10"])} in their last {n10} games.')
        n10s = tr["n_side10"]
        if n10s:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last10_side"])} in their last {n10s} {side_lbl} games.')

        if tr.get("streak_count", 0) >= 4:
            verb = "won" if tr["streak_type"] == "W" else "lost"
            lines.append(f'{_h(team)} have {verb} {tr["streak_count"]} straight.')

        n5 = tr["n_last5"]
        if n5:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last5"])} in {_h(sp_name)}\'s last {n5} starts.')
        n5s = tr["n_side5"]
        if n5s:
            lines.append(f'{_h(team)} are {_wl_s(*tr["last5_side"])} in {_h(sp_name)}\'s last {n5s} {side_lbl} starts.')
        if tr["avg_runs"] is not None and n5:
            lines.append(f'{_h(team)} average {tr["avg_runs"]:.1f} runs/game in {_h(sp_name)}\'s last {n5} starts.')
        if tr["avg_runs_side"] is not None and n5s:
            lines.append(f'{_h(team)} average {tr["avg_runs_side"]:.1f} runs/game in {_h(sp_name)}\'s last {n5s} {side_lbl} starts.')

        if not lines:
            return ""
        items = "".join(f"<li>{ln}</li>" for ln in lines)
        return f'<ul class="trends">{items}</ul>'

    away_tr = g.get("away_trends")
    home_tr = g.get("home_trends")

    def _trends_section(team: str, sp_name: str, tr, tid: str, is_away: bool) -> str:
        inner = _trend_block(team, sp_name, tr, is_away)
        if not inner.strip():
            return ""
        return (
            f'<details class="sec" id="{tid}">'
            f'<summary class="sec-sum">Trends · {_h(team)}</summary>'
            f'<div class="sec-body">{inner}</div>'
            f'</details>'
        )
    trends_html = (
        _trends_section(away, sp_a["name"], away_tr, f"{g_id}-trends-away", True)
        + _trends_section(home, sp_h["name"], home_tr, f"{g_id}-trends-home", False)
    )

    bullpen_html = (
        f'<details class="sec" id="{g_id}-bullpen">'
        f'<summary class="sec-sum">Bullpens · Last 12</summary>'
        f'<div class="sec-body">{_bp_row(away,bp_a)}{_bp_row(home,bp_h)}</div>'
        f'</details>'
    )

    ai_check = ""
    ai_sec_html = ""
    if ai_pick is not None:
        game_picks = ai_pick.get("picks") or []
        pass_reason = ai_pick.get("pass_reason", "")
        if game_picks:
            ai_check = '<span class="ai-check">✓</span>'
            sections = []
            for i, pick in enumerate(game_picks):
                sec_id = f'{g_id}-ai-{i}'
                sections.append(
                    f'<details class="sec ai-pick-card" id="{sec_id}">'
                    f'<summary class="sec-sum">{_h(_pick_summary_title(pick))}</summary>'
                    f'<div class="sec-body">'
                    f'<div class="ai-pick-inline">'
                    f'<div class="ai-reason">{_h(pick.get("reason",""))}</div>'
                    f'</div>'
                    f'</div>'
                    f'</details>'
                )
            ai_sec_html = "".join(sections)
        elif pass_reason:
            ai_sec_html = (
                f'<details class="sec" id="{g_id}-ai">'
                f'<summary class="sec-sum">AI Analysis</summary>'
                f'<div class="sec-body"><div class="ai-pass-reason">{_h(pass_reason)}</div></div>'
                f'</details>'
            )

    return (
        f'\n<details class="game" data-start-min="{_time_sort_key(g)}" id="{g_id}">'
        f'\n  <summary>'
        f'\n    <div class="gs-matchup"><div class="gs-teams">{logo_img(away)}{_h(away)} @ {logo_img(home)}{_h(home)}{ai_check}</div>{venue_html}</div>'
        f'\n  </summary>'
        f'\n  <div class="gd">'
        f'\n    {matchup_html}'
        f'\n    {odds_html}'
        f'\n    {outings_html}'
        f'\n    {splits_html}'
        f'\n    {trends_html}'
        f'\n    {bullpen_html}'
        f'\n    {wx_html}'
        f'\n    {flags_html}'
        f'\n    {ai_sec_html}'
        f'\n  </div>'
        f'\n</details>'
    )


# ── Full-page renderer ────────────────────────────────────────────────────────

def render_html_page(games: list[dict], target_date: date, generated_at: str,
                     odds_at: str = "", suggestions: Optional[dict] = None,
                     valid_picks: Optional[list] = None) -> str:
    date_long  = target_date.strftime(f"%A, %B {target_date.day}, %Y")
    date_short = target_date.strftime(f"%b {target_date.day}")
    games = sorted(games, key=_time_sort_key)
    valid_picks = valid_picks or []
    ai_by_game = _ai_game_map(valid_picks, suggestions)
    cards = "".join(_html_game(g, ai_by_game.get(f"{g['away']} @ {g['home']}")) for g in games)
    gen_span  = _ts_span(generated_at)
    odds_sub  = f" · Odds Updated {_ts_span(odds_at)}" if odds_at else ""
    ai_html   = _render_suggestions_html(valid_picks, target_date)
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>MLB Game Overviews · {_h(date_short)}</title>\n'
        f'<style>{_CSS}</style>\n'
        f'</head>\n<body>\n'
        f'<header><h1>MLB Game Overviews</h1>'
        f'<p class="sub">{_h(date_long)}</p>'
        f'<p class="sub">Updated {gen_span}{odds_sub}</p></header>\n'
        f'<main>{ai_html}{cards}\n</main>'
        f'<footer style="text-align:center;padding:1.5rem 1rem;font-size:.75rem;color:#9ca3af">'
        f'Powered by <a href="https://handigraphs.com" target="_blank" rel="noopener" style="color:#9ca3af">Handigraphs</a>'
        f'</footer>'
        f'{_SPLIT_SCRIPT}\n</body>\n</html>'
    )
