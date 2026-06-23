#!/usr/bin/env python3
# XAUUSD Signal-Notifier (Cloud, Einzellauf) -- wird per GitHub Actions alle paar Minuten gestartet.
# Holt PAXG/USDT-Kurse, berechnet das Signal (gleiche Logik wie das Dashboard),
# verwaltet den festen Trade-Status in state.json und schickt bei einem neuen Call
# eine Push-Nachricht an ntfy (-> Handy-App). Kein PC noetig.

import json, os, sys, urllib.request

TF      = "15m"
SYMBOL  = "PAXGUSDT"
HOSTS   = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
STATE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "").strip()

def fmt(x):
    return ("%0.2f" % x).replace(".", ",") if x is not None else "-"

# ---------- Daten ----------
def fetch_klines():
    last = None
    for host in HOSTS:
        try:
            url = f"{host}/api/v3/klines?symbol={SYMBOL}&interval={TF}&limit=180"
            req = urllib.request.Request(url, headers={"User-Agent": "xau-notifier"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = json.load(r)
            return [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4])} for k in raw]
        except Exception as e:
            last = e
    raise last

# ---------- Indikatoren ----------
def ema(vals, p):
    k = 2 / (p + 1); out = []; prev = None
    for i, v in enumerate(vals):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out

def rsi_last(c, p=14):
    if len(c) < p + 1: return 50.0
    g = l = 0.0
    for i in range(1, p + 1):
        d = c[i] - c[i - 1]
        if d >= 0: g += d
        else: l -= d
    ag = g / p; al = l / p
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]
        gg = d if d > 0 else 0; ll = -d if d < 0 else 0
        ag = (ag * (p - 1) + gg) / p; al = (al * (p - 1) + ll) / p
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def macd_last(c):
    f = ema(c, 12); s = ema(c, 26)
    line = [f[i] - s[i] for i in range(len(c))]
    sig = ema(line, 9); i = len(c) - 1
    return {"line": line[i], "signal": sig[i], "hist": line[i] - sig[i]}

def atr_last(h, l, c, p=14):
    tr = []
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(tr) < p: return sum(tr) / len(tr) if tr else 0
    atr = sum(tr[:p]) / p
    for i in range(p, len(tr)):
        atr = (atr * (p - 1) + tr[i]) / p
    return atr

def stoch_last(h, l, c, kP=14, dP=3):
    karr = []
    for i in range(kP - 1, len(c)):
        hh = max(h[i - kP + 1:i + 1]); ll = min(l[i - kP + 1:i + 1])
        karr.append(50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100)
    k = karr[-1]; d = sum(karr[-dP:]) / min(dP, len(karr))
    return {"k": k, "d": d}

def boll_last(c, p=20, m=2):
    s = c[-p:]; mean = sum(s) / len(s)
    sd = (sum((x - mean) ** 2 for x in s) / len(s)) ** 0.5
    return {"upper": mean + m * sd, "mid": mean, "lower": mean - m * sd}

def adx_last(h, l, c, p=14):
    pDM = []; mDM = []; tr = []
    for i in range(1, len(c)):
        up = h[i] - h[i - 1]; dn = l[i - 1] - l[i]
        pDM.append(up if (up > dn and up > 0) else 0)
        mDM.append(dn if (dn > up and dn > 0) else 0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(tr) < p + 1: return 0
    def sm(arr):
        out = [None] * len(arr); s = sum(arr[:p]); out[p - 1] = s
        for i in range(p, len(arr)):
            s = s - s / p + arr[i]; out[i] = s
        return out
    trS = sm(tr); pS = sm(pDM); mS = sm(mDM); dx = []
    for i in range(p - 1, len(tr)):
        if trS[i] in (None, 0): continue
        pdi = 100 * pS[i] / trS[i]; mdi = 100 * mS[i] / trS[i]; ssum = pdi + mdi
        dx.append(0 if ssum == 0 else 100 * abs(pdi - mdi) / ssum)
    if not dx: return 0
    if len(dx) < p: return dx[-1]
    adx = sum(dx[:p]) / p
    for i in range(p, len(dx)):
        adx = (adx * (p - 1) + dx[i]) / p
    return adx

def find_pivots(h, l, win=3):
    res = []; sup = []
    for i in range(win, len(h) - win):
        isH = isL = True
        for j in range(i - win, i + win + 1):
            if h[j] > h[i]: isH = False
            if l[j] < l[i]: isL = False
        if isH: res.append(h[i])
        if isL: sup.append(l[i])
    return res, sup

def cluster(levels, tol):
    arr = sorted(levels); out = []
    for v in arr:
        if out:
            avg = out[-1][0] / out[-1][1]
            if abs(v - avg) / avg < tol:
                out[-1][0] += v; out[-1][1] += 1; continue
        out.append([v, 1])
    return [o[0] / o[1] for o in out]

def support_resistance(h, l, price):
    pr, ps = find_pivots(h, l, 3); tol = 0.002
    res = sorted([v for v in cluster(pr, tol) if v > price * 1.0003])[:3]
    sup = sorted([v for v in cluster(ps, tol) if v < price * 0.9997], reverse=True)[:3]
    near_res = len(res) > 0 and (res[0] - price) / price < 0.004
    near_sup = len(sup) > 0 and (price - sup[0]) / price < 0.004
    return {"res": res, "sup": sup, "nearRes": near_res, "nearSup": near_sup}

def detect_pattern(cd):
    n = len(cd) - 1
    if n < 2: return "neutral"
    c0 = cd[n]; c1 = cd[n - 1]
    body = abs(c0["c"] - c0["o"]); rng = (c0["h"] - c0["l"]) or 1e-9
    upW = c0["h"] - max(c0["c"], c0["o"]); loW = min(c0["c"], c0["o"]) - c0["l"]
    bull = c0["c"] > c0["o"]; bear = c0["c"] < c0["o"]; prev = abs(c1["c"] - c1["o"])
    if bull and c1["c"] < c1["o"] and c0["c"] >= c1["o"] and c0["o"] <= c1["c"] and body > prev * 0.9: return "bull"
    if bear and c1["c"] > c1["o"] and c0["o"] >= c1["c"] and c0["c"] <= c1["o"] and body > prev * 0.9: return "bear"
    if loW > body * 2 and upW < body * 0.7: return "bull"
    if upW > body * 2 and loW < body * 0.7: return "bear"
    if body <= rng * 0.1: return "neutral"
    if body >= rng * 0.8: return "bull" if bull else "bear"
    return "bull" if bull else ("bear" if bear else "neutral")

# ---------- Signal ----------
def compute_signal(cd):
    closes = [x["c"] for x in cd]; highs = [x["h"] for x in cd]; lows = [x["l"] for x in cd]
    price = closes[-1]
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    ema20 = e20[-1]; ema50 = e50[-1]
    rsi = rsi_last(closes); macd = macd_last(closes); atr = atr_last(highs, lows, closes)
    st = stoch_last(highs, lows, closes); bo = boll_last(closes); adx = adx_last(highs, lows, closes)
    sr = support_resistance(highs, lows, price); pat = detect_pattern(cd)
    sc = 0.0
    sc += 2 if ema20 > ema50 else -2
    sc += 1 if price > ema20 else -1
    sc += 1.5 if macd["hist"] > 0 else -1.5
    if rsi >= 70: sc -= 1
    elif rsi > 55: sc += 1
    elif rsi <= 30: sc += 0.5
    elif rsi < 45: sc -= 1
    if st["k"] < 20: sc += 0.5
    elif st["k"] > 80: sc -= 0.5
    else: sc += 0.3 if st["k"] > st["d"] else -0.3
    if price < bo["lower"]: sc += 0.5
    elif price > bo["upper"]: sc -= 0.5
    if sr["nearSup"]: sc += 0.5
    if sr["nearRes"]: sc -= 0.5
    if pat == "bull": sc += 0.5
    elif pat == "bear": sc -= 0.5
    direction = "buy" if sc >= 3 else ("sell" if sc <= -3 else "neutral")
    entry = price; sl = tp = None
    if direction == "buy": sl = price - 1.5 * atr; tp = price + 3 * atr
    elif direction == "sell": sl = price + 1.5 * atr; tp = price - 3 * atr
    adxF = 1.15 if adx >= 25 else (0.8 if adx < 20 else 1.0)
    conf = min(100, round(abs(sc) / 7 * 100 * adxF))
    return {"price": price, "dir": direction, "entry": entry, "sl": sl, "tp": tp, "conf": conf}

def backtest(cd):
    start = 60
    if len(cd) < start + 5: return {"total": 0, "winRate": 0}
    highs = [x["h"] for x in cd]; lows = [x["l"] for x in cd]
    wins = losses = 0; i = start
    while i < len(cd) - 1:
        sig = compute_signal(cd[:i + 1])
        if sig["dir"] == "neutral": i += 1; continue
        d = sig["dir"]; sl = sig["sl"]; tp = sig["tp"]; outcome = None; j = i + 1
        while j < len(cd):
            if d == "buy":
                if lows[j] <= sl: outcome = "loss"; break
                if highs[j] >= tp: outcome = "win"; break
            else:
                if highs[j] >= sl: outcome = "loss"; break
                if lows[j] <= tp: outcome = "win"; break
            j += 1
        if outcome is None: break
        if outcome == "win": wins += 1
        else: losses += 1
        i = j + 1
    total = wins + losses
    return {"total": total, "winRate": round(wins / total * 100) if total else 0}

# ---------- Push ----------
def push(title, body, tags, priority="high"):
    if not NTFY_TOPIC:
        print("FEHLER: NTFY_TOPIC ist nicht gesetzt (GitHub-Secret fehlt).")
        return
    try:
        req = urllib.request.Request(
            NTFY_SERVER + "/" + NTFY_TOPIC,
            data=body.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": priority})
        urllib.request.urlopen(req, timeout=20).read()
        print("Push gesendet:", title)
    except Exception as e:
        print("Push-Fehler:", e)

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {"active": None, "closeT": 0}

def save_state(s):
    with open(STATE, "w") as f: json.dump(s, f)

# ---------- Hauptlauf ----------
def main():
    cd = fetch_klines()
    now_t = cd[-1]["t"]; price = cd[-1]["c"]
    sig = compute_signal(cd)
    st = load_state()
    active = st.get("active"); close_t = st.get("closeT", 0)

    # offenen Trade pruefen
    if active:
        closed = None
        if active["dir"] == "buy":
            if price <= active["sl"]: closed = "loss"
            elif price >= active["tp"]: closed = "win"
        else:
            if price >= active["sl"]: closed = "loss"
            elif price <= active["tp"]: closed = "win"
        if closed:
            side = "KAUF" if active["dir"] == "buy" else "VERKAUF"
            if closed == "win":
                push("Take-Profit erreicht", "✅ " + side + " gewonnen bei " + fmt(price) + " USD", "white_check_mark")
            else:
                push("Stop-Loss erreicht", "❌ " + side + " verloren bei " + fmt(price) + " USD", "x")
            close_t = now_t; active = None

    # neuen Call eroeffnen (nicht auf derselben Kerze wie ein Close)
    if active is None and sig["dir"] != "neutral" and now_t > close_t:
        bt = backtest(cd)
        base = bt["winRate"] if bt["total"] >= 3 else 50
        prob = round(max(5, min(95, 0.55 * base + 0.45 * sig["conf"])))
        active = {"dir": sig["dir"], "entry": sig["entry"], "sl": sig["sl"],
                  "tp": sig["tp"], "t": now_t, "conf": sig["conf"], "prob": prob}
        body = ("Einstieg " + fmt(sig["entry"]) + "\nSL " + fmt(sig["sl"]) +
                "\nTP " + fmt(sig["tp"]) + "\nErfolgs-Chance " + str(prob) + "%")
        if sig["dir"] == "buy":
            push("🟢 KAUF-Signal XAUUSD", "🟢 KAUFEN\n" + body, "green_circle")
        else:
            push("🔴 VERKAUF-Signal XAUUSD", "🔴 VERKAUFEN\n" + body, "red_circle")

    save_state({"active": active, "closeT": close_t})
    print("Kurs", fmt(price), "| Signal", sig["dir"], "| offen:",
          (active["dir"] if active else "-"))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Lauf-Fehler:", e); sys.exit(0)  # Exit 0, damit der Cron-Job nicht als fehlgeschlagen gilt
