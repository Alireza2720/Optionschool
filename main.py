"""
OptionSchool - FastAPI Backend
همه چیز اینجاست: API proxy + محاسبات + serve فایل HTML
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import httpx
from datetime import datetime
import math
import os

app = FastAPI(title="OptionSchool", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== CACHE ====================
_cache = {"data": None, "ts": None}
CACHE_TTL = 60

def safe_float(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def safe_int(v, d=0):
    try: return int(v) if v is not None else d
    except: return d

# ==================== FETCH ====================
async def get_contracts():
    global _cache
    now = datetime.now()
    if _cache["data"] and _cache["ts"] and (now - _cache["ts"]).seconds < CACHE_TTL:
        return _cache["data"]

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://s3.optionschool24.com/last?type=3")
        r.raise_for_status()
        raw = r.json()

    items = raw if isinstance(raw, list) else raw.get("data", [])
    contracts = []
    for row in items:
        try:
            s  = safe_float(row.get("basis"))
            k  = safe_float(row.get("emal"))
            p  = safe_float(row.get("close"))
            iv = safe_float(row.get("value"))
            t  = safe_int(row.get("type", 1))
            dte = safe_int(row.get("day_left"))
            bid = safe_float(row.get("bid_price") or row.get("bid"))
            ask = safe_float(row.get("ask_price") or row.get("ask"))

            if t == 1:
                iv_calc  = max(0, s - k)
                be       = k + p
                status   = "در سود" if s > k else ("بی‌تفاوت" if s == k else "در ضرر")
            else:
                iv_calc  = max(0, k - s)
                be       = k - p
                status   = "در سود" if s < k else ("بی‌تفاوت" if s == k else "در ضرر")

            contracts.append({
                "name":             row.get("name",""),
                "basis_name":       row.get("basis_name",""),
                "type":             "call" if t==1 else "put",
                "strike":           k,
                "spot":             s,
                "price":            p,
                "bid":              bid,
                "ask":              ask,
                "bid_vol":          safe_int(row.get("bid_volume")),
                "ask_vol":          safe_int(row.get("ask_volume")),
                "iv":               iv_calc,
                "tv":               max(0, p - iv_calc),
                "dte":              dte,
                "size":             safe_int(row.get("size",1000)),
                "break_even":       round(be,0),
                "be_diff_pct":      round((be-s)/s*100,2) if s>0 else 0,
                "leverage":         round(s/p,1) if p>0 else 0,
                "status":           status,
                "expiry":           row.get("date",""),
                "delta":            safe_float(row.get("delta")),
                "theta":            safe_float(row.get("theta")),
                "gamma":            safe_float(row.get("gamma")),
                "vega":             safe_float(row.get("vega")),
                "oi":               safe_float(row.get("open_interest")),
                "volume":           safe_float(row.get("volume")),
            })
        except:
            continue

    _cache = {"data": contracts, "ts": now}
    return contracts

# ==================== NORMALISE ====================
def norm30(pct, dte):
    if dte <= 0: return pct
    return round(pct * 30 / dte, 2)

# ==================== STRATEGIES ====================
def covered_call(cs):
    out = []
    for c in cs:
        if c["type"]!="call" or c["bid"]<=0 or c["spot"]<=0 or c["dte"]<=0: continue
        s,k,bid,dte = c["spot"],c["strike"],c["bid"],c["dte"]
        cost = s - bid
        if cost<=0: continue
        max_p = k - s + bid
        if max_p<=0: continue
        roi = norm30(max_p/cost*100, dte)
        out.append({**c,
            "premium": bid,
            "cost": round(cost),
            "break_even": round(s-bid),
            "max_profit_pct": roi,
            "max_loss_pct": round(-cost/cost*100, 2),  # -100% (کل سهم از دست میره)
            "payoff": build_payoff_cc(s, k, bid),
        })
    return sorted(out, key=lambda x: x["max_profit_pct"], reverse=True)

def married_put(cs):
    out = []
    for c in cs:
        if c["type"]!="put" or c["ask"]<=0 or c["spot"]<=0 or c["dte"]<=0: continue
        s,k,ask,dte = c["spot"],c["strike"],c["ask"],c["dte"]
        total = s + ask
        max_loss = -(s - k + ask) if k<s else -ask
        ml_pct = max_loss/total*100
        out.append({**c,
            "premium": ask,
            "total_cost": round(total),
            "break_even": round(s+ask),
            "max_loss_pct": round(ml_pct,2),
            "payoff": build_payoff_mp(s, k, ask),
        })
    return sorted(out, key=lambda x: x["max_loss_pct"], reverse=True)

def collar(cs):
    calls = {(c["basis_name"],c["expiry"]):c for c in cs if c["type"]=="call" and c["bid"]>0}
    out = []
    for p in cs:
        if p["type"]!="put" or p["ask"]<=0 or p["dte"]<=0: continue
        c = calls.get((p["basis_name"],p["expiry"]))
        if not c or c["strike"]<=p["strike"]: continue
        s,kp,kc = p["spot"],p["strike"],c["strike"]
        net = c["bid"]-p["ask"]
        cost = s-net
        if cost<=0: continue
        max_p = kc-s+net
        max_l = -(s-kp-net)
        if max_p<=0: continue
        roi = norm30(max_p/cost*100, p["dte"])
        out.append({
            "basis_name": p["basis_name"],
            "put_name": p["name"], "call_name": c["name"],
            "spot": s, "k_put": kp, "k_call": kc,
            "net_premium": round(net), "cost": round(cost),
            "break_even": round(s-net),
            "max_profit_pct": roi,
            "max_loss_pct": round(max_l/cost*100,2),
            "dte": p["dte"], "expiry": p["expiry"],
            "payoff": build_payoff_collar(s,kp,kc,net),
        })
    return sorted(out, key=lambda x: x["max_profit_pct"], reverse=True)

def bull_call_spread(cs):
    grp = {}
    for c in cs:
        if c["type"]!="call": continue
        k=(c["basis_name"],c["expiry"])
        grp.setdefault(k,[]).append(c)
    out=[]
    for (basis,exp), g in grp.items():
        g.sort(key=lambda x: x["strike"])
        for i in range(len(g)-1):
            buy,sell = g[i],g[i+1]
            pb,ps = buy["ask"],sell["bid"]
            if pb<=0 or ps<=0 or buy["ask_vol"]<=0 or sell["bid_vol"]<=0: continue
            net=pb-ps
            if net<=0: continue
            spread=sell["strike"]-buy["strike"]
            mp=spread-net
            if mp<=0: continue
            roi=norm30(mp/net*100, buy["dte"])
            out.append({
                "basis_name":basis,
                "buy_name":buy["name"],"sell_name":sell["name"],
                "k_buy":buy["strike"],"k_sell":sell["strike"],
                "spot":buy["spot"],"net_cost":round(net),
                "break_even":round(buy["strike"]+net),
                "max_profit":round(mp),"max_loss":round(-net),
                "max_profit_pct":roi,
                "dte":buy["dte"],"expiry":exp,
                "payoff": build_payoff_spread(buy["strike"],sell["strike"],net,"call"),
            })
    return sorted(out,key=lambda x:x["max_profit_pct"],reverse=True)[:100]

def put_spread(cs):
    grp = {}
    for c in cs:
        if c["type"]!="put": continue
        k=(c["basis_name"],c["expiry"])
        grp.setdefault(k,[]).append(c)
    out=[]
    for (basis,exp), g in grp.items():
        g.sort(key=lambda x: x["strike"], reverse=True)
        for i in range(len(g)-1):
            buy,sell = g[i],g[i+1]  # buy=high strike, sell=low strike
            pb,ps = buy["ask"],sell["bid"]
            if pb<=0 or ps<=0: continue
            net=pb-ps
            if net<=0: continue
            spread=buy["strike"]-sell["strike"]
            mp=spread-net
            if mp<=0: continue
            roi=norm30(mp/net*100, buy["dte"])
            out.append({
                "basis_name":basis,
                "buy_name":buy["name"],"sell_name":sell["name"],
                "k_buy":buy["strike"],"k_sell":sell["strike"],
                "spot":buy["spot"],"net_cost":round(net),
                "break_even":round(buy["strike"]-net),
                "max_profit":round(mp),"max_loss":round(-net),
                "max_profit_pct":roi,
                "dte":buy["dte"],"expiry":exp,
                "payoff": build_payoff_spread(sell["strike"],buy["strike"],net,"put"),
            })
    return sorted(out,key=lambda x:x["max_profit_pct"],reverse=True)[:100]

def straddle(cs):
    call_map,put_map={},{}
    for c in cs:
        k=(c["basis_name"],c["expiry"],c["strike"])
        if c["type"]=="call" and c["ask"]>0: call_map[k]=c
        elif c["type"]=="put" and c["ask"]>0: put_map[k]=c
    out=[]
    for k,call in call_map.items():
        put=put_map.get(k)
        if not put or call["dte"]<=0: continue
        s=call["spot"]; kk=call["strike"]
        tc=call["ask"]+put["ask"]
        out.append({
            "basis_name":call["basis_name"],
            "call_name":call["name"],"put_name":put["name"],
            "strike":kk,"spot":s,"total_cost":round(tc),
            "be_up":round(kk+tc),"be_down":round(kk-tc),
            "move_needed_pct":round(tc/s*100,2) if s>0 else 0,
            "max_loss":round(-tc),
            "dte":call["dte"],"expiry":call["expiry"],
            "payoff": build_payoff_straddle(s,kk,tc),
        })
    return sorted(out,key=lambda x:x["move_needed_pct"])[:80]

def strangle(cs):
    grp={}
    for c in cs:
        k=(c["basis_name"],c["expiry"])
        grp.setdefault(k,{"calls":[],"puts":[]})
        if c["type"]=="call" and c["ask"]>0: grp[k]["calls"].append(c)
        elif c["type"]=="put" and c["ask"]>0:  grp[k]["puts"].append(c)
    out=[]
    for (basis,exp),g in grp.items():
        if not g["calls"] or not g["puts"]: continue
        s = g["calls"][0]["spot"] if g["calls"] else 0
        if s<=0: continue
        otmc=[c for c in g["calls"] if c["strike"]>s]
        otmp=[p for p in g["puts"]  if p["strike"]<s]
        if not otmc or not otmp: continue
        call=min(otmc,key=lambda x:x["strike"])
        put =max(otmp,key=lambda x:x["strike"])
        tc=call["ask"]+put["ask"]
        if call["dte"]<=0: continue
        out.append({
            "basis_name":basis,
            "call_name":call["name"],"put_name":put["name"],
            "k_call":call["strike"],"k_put":put["strike"],
            "spot":s,"total_cost":round(tc),
            "be_up":round(call["strike"]+tc),"be_down":round(put["strike"]-tc),
            "move_needed_pct":round(tc/s*100,2) if s>0 else 0,
            "dte":call["dte"],"expiry":exp,
            "payoff": build_payoff_strangle(s,call["strike"],put["strike"],tc),
        })
    return sorted(out,key=lambda x:x["move_needed_pct"])[:80]

def conversion(cs):
    """Conversion: خرید پوت + فروش کال (همان اعمال) + خرید سهم"""
    call_map={}
    for c in cs:
        if c["type"]=="call" and c["bid"]>0:
            call_map[(c["basis_name"],c["expiry"],c["strike"])]=c
    out=[]
    for p in cs:
        if p["type"]!="put" or p["ask"]<=0 or p["dte"]<=0: continue
        k=(p["basis_name"],p["expiry"],p["strike"])
        c=call_map.get(k)
        if not c: continue
        s,kk=p["spot"],p["strike"]
        net=c["bid"]-p["ask"]   # پرمیوم دریافتی - پرمیوم پرداختی
        # سود: اگر سهم < اعمال، پوت اعمال می‌شه و سود = kk-s+net
        # در بازار کارآ سود باید نزدیک صفر باشه
        pnl = kk - s + net   # سود نظری آربیتراژ
        cost = s - net
        if cost<=0: continue
        roi=norm30(pnl/cost*100, p["dte"]) if cost>0 else 0
        out.append({
            "basis_name":p["basis_name"],
            "put_name":p["name"],"call_name":c["name"],
            "strike":kk,"spot":s,
            "call_bid":c["bid"],"put_ask":p["ask"],
            "net_premium":round(net),
            "theoretical_pnl":round(pnl),
            "roi_30":roi,
            "dte":p["dte"],"expiry":p["expiry"],
            "payoff": build_payoff_conversion(s,kk,net),
        })
    return sorted(out,key=lambda x:x["roi_30"],reverse=True)[:80]

def box(cs):
    """Long Box: Bull Call Spread + Bear Put Spread (همان اعمال)"""
    grp={}
    for c in cs:
        k=(c["basis_name"],c["expiry"])
        grp.setdefault(k,{"calls":[],"puts":[]})
        if c["type"]=="call": grp[k]["calls"].append(c)
        else: grp[k]["puts"].append(c)
    out=[]
    for (basis,exp),g in grp.items():
        calls=sorted(g["calls"],key=lambda x:x["strike"])
        puts =sorted(g["puts"], key=lambda x:x["strike"])
        if len(calls)<2 or len(puts)<2: continue
        for i in range(len(calls)-1):
            for j in range(len(puts)-1):
                cl=calls[i]; ch=calls[i+1]
                ph=puts[j];  pl=puts[j+1]
                if cl["strike"]!=pl["strike"] or ch["strike"]!=ph["strike"]: continue
                kl,kh=cl["strike"],ch["strike"]
                if kl>=kh: continue
                # هزینه کل
                cost=(cl["ask"]-ch["bid"])+(ph["ask"]-pl["bid"])
                if cost<=0: continue
                box_value=kh-kl
                profit=box_value-cost
                roi=norm30(profit/cost*100, cl["dte"]) if cost>0 else 0
                if cl["dte"]<=0: continue
                out.append({
                    "basis_name":basis,
                    "cl_name":cl["name"],"ch_name":ch["name"],
                    "ph_name":ph["name"],"pl_name":pl["name"],
                    "k_low":kl,"k_high":kh,
                    "spot":cl["spot"],
                    "cost":round(cost),
                    "box_value":round(box_value),
                    "max_profit":round(profit),
                    "roi_30":roi,
                    "dte":cl["dte"],"expiry":exp,
                    "payoff": build_payoff_box(kl,kh,cost),
                })
    return sorted(out,key=lambda x:x["roi_30"],reverse=True)[:80]

# ==================== PAYOFF BUILDERS ====================
def pts(spot, lo, hi, n=30):
    step=(hi-lo)/n
    return [round(lo+i*step) for i in range(n+1)]

def build_payoff_cc(s,k,premium):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    ys=[round(min(k,x)-(s-premium),0) for x in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_mp(s,k,premium):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    ys=[round(max(k,x)-(s+premium),0) for x in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_collar(s,kp,kc,net):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    ys=[round(min(kc,max(kp,x))-s+net,0) for x in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_spread(kl,kh,cost,typ):
    lo,hi=kl*0.8,kh*1.2
    xs=pts(None,lo,hi)
    if typ=="call":
        ys=[round(min(max(0,x-kl),kh-kl)-cost,0) for x in xs]
    else:
        ys=[round(min(max(0,kh-x),kh-kl)-cost,0) for x in xs]
    return {"x":xs,"y":ys,"spot":(kl+kh)/2}

def build_payoff_straddle(s,k,tc):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    ys=[round(abs(x-k)-tc,0) for x in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_strangle(s,kc,kp,tc):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    ys=[round(max(0,x-kc)+max(0,kp-x)-tc,0) for x in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_conversion(s,k,net):
    lo,hi=s*0.7,s*1.3
    xs=pts(s,lo,hi)
    # سود ثابت = k-s+net در همه حالات
    fixed=k-s+net
    ys=[round(fixed,0) for _ in xs]
    return {"x":xs,"y":ys,"spot":s}

def build_payoff_box(kl,kh,cost):
    lo,hi=kl*0.8,kh*1.2
    xs=pts(None,lo,hi)
    fixed=kh-kl-cost
    ys=[round(fixed,0) for _ in xs]
    return {"x":xs,"y":ys,"spot":(kl+kh)/2}

# ==================== ENDPOINTS ====================
@app.get("/api/contracts")
async def api_contracts():
    try:
        cs=await get_contracts()
        return {"ok":True,"count":len(cs),"data":cs}
    except Exception as e:
        raise HTTPException(500,str(e))

@app.get("/api/all")
async def api_all():
    try:
        cs=await get_contracts()
        return {"ok":True,"updated":datetime.now().isoformat(),"data":{
            "contracts":    cs,
            "covered_call": covered_call(cs),
            "married_put":  married_put(cs),
            "collar":       collar(cs),
            "bull_call":    bull_call_spread(cs),
            "put_spread":   put_spread(cs),
            "straddle":     straddle(cs),
            "strangle":     strangle(cs),
            "conversion":   conversion(cs),
            "box":          box(cs),
        }}
    except Exception as e:
        raise HTTPException(500,str(e))

@app.get("/api/health")
async def health():
    return {"ok":True,"time":datetime.now().isoformat()}

# serve فایل HTML از همین سرور
FRONTEND = os.path.join(os.path.dirname(__file__),"..","frontend","index.html")

@app.get("/")
async def root():
    if os.path.exists(FRONTEND):
        return FileResponse(FRONTEND)
    return HTMLResponse("<h1>frontend/index.html not found</h1>")
