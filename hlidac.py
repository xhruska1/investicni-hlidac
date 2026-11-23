"""Denní hlídač investic – jen upozornění, žádné obchodování.

Použití:
  python hlidac.py             běžný denní běh (posílá jen když se něco splní)
  python hlidac.py --dry-run   nic neposílá, nic neukládá, jen vypíše
  python hlidac.py --test      pošle testovací notifikaci s aktuálními čísly
  python hlidac.py --date 2026-12-01   simuluje jiné datum (pro testy pravidel)
  python hlidac.py --daily-after 9     poběží jen jednou denně a až po 9:00 pražského času
                                       (pro GitHub Actions, kde cron běží v UTC)
"""
import argparse
import base64
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
if not CONFIG.exists():  # na GitHubu je jen ukázkový config, skutečný zůstává lokálně
    CONFIG = BASE / "config.example.json"
STATE = BASE / "state.json"
LOG = BASE / "hlidac.log"

# měna instrumentu -> FX pár do CZK na Yahoo
FX = {"EUR": "EURCZK=X", "USD": "USDCZK=X", "GBP": "GBPCZK=X", "GBp": "GBPCZK=X", "CZK": None}

log = logging.getLogger("hlidac")


# ---------- data ----------

def close_series(ticker):
    for attempt in range(3):
        h = yf.Ticker(ticker).history(period="max", auto_adjust=False)
        if not h.empty:
            break
        time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"Žádná data pro {ticker}")
    s = h["Close"].dropna()
    s.index = s.index.tz_localize(None).normalize()
    return s.groupby(level=0).last()


def czk_series(ticker, fx_cache):
    currency = yf.Ticker(ticker).fast_info.get("currency") or "USD"
    s = close_series(ticker)
    if currency == "GBp":
        s = s / 100
    pair = FX.get(currency)
    if pair is None and currency != "CZK":
        raise RuntimeError(f"Neznámá měna {currency} u {ticker}")
    if pair:
        if pair not in fx_cache:
            fx_cache[pair] = close_series(pair)
        fx = fx_cache[pair].reindex(s.index.union(fx_cache[pair].index)).ffill().reindex(s.index)
        s = (s * fx).dropna()
    return s, currency


def metrics(s, today):
    s = s[s.index <= pd.Timestamp(today)]
    price = float(s.iloc[-1])
    ath = float(s.max())
    last_year = s[s.index > s.index[-1] - pd.Timedelta(days=365)]
    month_ago = s[s.index <= s.index[-1] - pd.DateOffset(months=1)]
    chg_1m = (price / float(month_ago.iloc[-1]) - 1) * 100 if len(month_ago) else None
    return {
        "date": s.index[-1].date().isoformat(),
        "price_czk": round(price, 2),
        "ath_czk": round(ath, 2),
        "ath_date": s.idxmax().date().isoformat(),
        "pct_below_ath": round((1 - price / ath) * 100, 2),
        "low_52w_czk": round(float(last_year.min()), 2),
        "high_52w_czk": round(float(last_year.max()), 2),
        "chg_1m_pct": None if chg_1m is None else round(chg_1m, 2),
    }


# ---------- stav ----------

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8-sig"))
    return {}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- pravidla ----------

def evaluate(cfg, data, month_state, today):
    """Vrátí seznam signálů. month_state se upravuje jen v paměti."""
    lim = cfg["limits"]
    signals = []
    sent = set(month_state.setdefault("sent", []))

    # 1) měsíční základní nákup – první běh v měsíci od from_day
    mr = cfg.get("monthly_reminder", {})
    if mr.get("enabled", True) and today.day >= mr.get("from_day", 1) and "base" not in sent:
        items = []
        for a in cfg["assets"]:
            amt = min(a.get("monthly_base_czk", 0), lim["max_per_buy_czk"])
            if amt > 0:
                items.append((a, amt))
        if items:
            total = sum(x for _, x in items)
            signals.append({"key": "base", "kind": "base", "items": items, "amount": total,
                            "counts": lim.get("base_counts_toward_monthly_limit", False)})

    # 2) propady pod ATH – nejvyšší splněný a dosud neposlaný stupeň
    used = month_state.get("proposed_czk", 0)
    if signals and signals[0]["counts"]:
        used += signals[0]["amount"]
    for a in cfg["assets"]:
        m = data.get(a["id"])
        if not m:
            continue
        hit = [r for r in sorted(a["dip_rules"], key=lambda r: r["pct_below_ath"])
               if m["pct_below_ath"] >= r["pct_below_ath"]]
        if not hit:
            continue
        top = hit[-1]
        key = f'{a["id"]}:{top["pct_below_ath"]}'
        if key in sent:
            continue
        remaining = lim["max_per_month_czk"] - used
        amt = min(top["amount_czk"], lim["max_per_buy_czk"], remaining)
        if amt <= 0:
            log.info("Signál %s přeskočen – vyčerpán měsíční limit", key)
            continue
        used += amt
        signals.append({"key": key, "kind": "dip", "asset": a, "rule": top, "amount": amt,
                        "capped": amt < top["amount_czk"],
                        # nižší stupně se tímto považují za vyřízené
                        "also_done": [f'{a["id"]}:{r["pct_below_ath"]}' for r in hit[:-1]]})
    return signals


# ---------- texty ----------

def fmt(x):
    return f"{x:,.0f}".replace(",", " ")


KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}


def claude_cmd(cfg):
    path = os.environ.get("CLAUDE_CMD") or cfg.get("claude_cmd")
    if path and Path(path).exists():
        return path
    return shutil.which("claude") or path


def claude_comment(cfg, prompt):
    # nedědit proměnné z případné nadřazené Claude session (token ale ponechat)
    env = {k: v for k, v in os.environ.items()
           if k in KEEP_ENV or not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC_"))}
    try:
        r = subprocess.run([claude_cmd(cfg), "-p", prompt], capture_output=True, text=True,
                           encoding="utf-8", timeout=cfg.get("claude_timeout_s", 180), env=env,
                           cwd=BASE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = " ".join(r.stdout.split())
        if r.returncode != 0 or not out:
            log.warning("claude -p selhal (%s): %s", r.returncode, (r.stderr or r.stdout).strip()[:300])
            return None
        return out
    except Exception as e:
        log.warning("claude -p nešel spustit: %s", e)
        return None


PROMPT_HEAD = ("Jsi stručný komentátor pro soukromého investora (dlouhodobé pravidelné investování, "
               "žádné obchodování). Napiš ČESKY 2-3 krátké věty (celkem max 220 znaků) k následujícímu "
               "upozornění. Vycházej jen z dodaných čísel, nic si nevymýšlej, nedávej záruky ani "
               "doporučení nad rámec pravidla. Vrať jen samotný text, bez nadpisu a odrážek.\n\n")


def describe(name, m):
    chg = "n/a" if m["chg_1m_pct"] is None else f'{m["chg_1m_pct"]:+.1f} %'
    return (f"{name}: cena {fmt(m['price_czk'])} Kč (k {m['date']}), ATH {fmt(m['ath_czk'])} Kč "
            f"({m['ath_date']}), {m['pct_below_ath']:.1f} % pod ATH, 52t min {fmt(m['low_52w_czk'])} / "
            f"max {fmt(m['high_52w_czk'])} Kč, změna 1M {chg}")


def layout(co, kolik, proc, comment=None):
    """Bloky oddělené prázdným řádkem; kolik/proc mohou být seznamy řádků."""
    as_list = lambda x: x if isinstance(x, list) else [x]
    blocks = ["📌 " + co,
              "💰 " + "\n".join(as_list(kolik)),
              "❓ " + "\n".join(as_list(proc))]
    if comment:
        blocks.append("💬 " + comment)
    return "\n\n".join(blocks)


def stats_line(m):
    chg = "n/a" if m["chg_1m_pct"] is None else f'{m["chg_1m_pct"]:+.1f} %'
    return f'1M: {chg} · 52t: {fmt(m["low_52w_czk"])}–{fmt(m["high_52w_czk"])} Kč'


def build_message(cfg, sig, data):
    if sig["kind"] == "base":
        title = "Měsíční nákup – připomínka"
        co = f"Pravidelný měsíční nákup"
        kolik = [f"Celkem {fmt(sig['amount'])} Kč"] + \
                [f'• {a["name"]}: {fmt(x)} Kč' for a, x in sig["items"]]
        proc = "Začátek měsíce, základní plán"
        ctx = "\n".join(describe(a["name"], data[a["id"]]) for a, _ in sig["items"] if a["id"] in data)
        prompt = PROMPT_HEAD + "Upozornění: připomínka pravidelného měsíčního nákupu (" + \
            ", ".join(f'{a["name"]} {x} Kč' for a, x in sig["items"]) + ").\nAktuální data:\n" + ctx
    else:
        a, m, r = sig["asset"], data[sig["asset"]["id"]], sig["rule"]
        title = f'{a["name"]}: {m["pct_below_ath"]:.0f} % pod ATH'
        cap = " (zkráceno limitem)" if sig["capped"] else ""
        co = f'Přikoupit {a["name"]} ({a["ticker"]})'
        kolik = f'{fmt(sig["amount"])} Kč{cap}'
        proc = [f'{m["pct_below_ath"]:.1f} % pod ATH (pravidlo ≥{r["pct_below_ath"]} %)',
                f'Cena {fmt(m["price_czk"])} Kč · ATH {fmt(m["ath_czk"])} Kč',
                stats_line(m)]
        prompt = PROMPT_HEAD + (f'Upozornění: {a["name"]} je {m["pct_below_ath"]:.1f} % pod historickým '
                                f'maximem v CZK, pravidlo ≥{r["pct_below_ath"]} % navrhuje nákup za '
                                f'{sig["amount"]} Kč.\nData: ' + describe(a["name"], m))
    return title, layout(co, kolik, proc, claude_comment(cfg, prompt))


def send_ntfy(cfg, title, body, tags="chart_with_upwards_trend"):
    enc_title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?="
    url = os.environ.get("NTFY_URL") or cfg["ntfy_url"]  # tajné téma ntfy lze předat přes proměnnou prostředí
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                 headers={"Title": enc_title, "Tags": tags,
                                          "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        log.info("ntfy %s: %s", resp.status, title)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--daily-after", type=int, metavar="HODINA")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG, encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    now = dt.datetime.now(ZoneInfo("Europe/Prague"))
    today = dt.date.fromisoformat(args.date) if args.date else now.date()

    state = load_state()
    if args.daily_after is not None and not args.test:
        if now.hour < args.daily_after or state.get("last_run") == today.isoformat():
            log.info("Dnes už proběhlo nebo je před %d:00 – končím.", args.daily_after)
            return
        if not args.dry_run:
            state["last_run"] = today.isoformat()
            save_state(state)

    data, fx_cache = {}, {}
    for a in cfg["assets"]:
        try:
            s, cur = czk_series(a["ticker"], fx_cache)
            data[a["id"]] = metrics(s, today)
            log.info("%s", describe(f'{a["name"]} [{a["ticker"]}, {cur}]', data[a["id"]]))
        except Exception as e:
            log.error("Chyba u %s: %s", a["ticker"], e)
    if not data:
        raise RuntimeError("Nepodařilo se stáhnout data pro žádné aktivum")

    if args.test:
        a = cfg["assets"][0]
        proc, comment = ["Ověření, že notifikace fungují"], None
        if a["id"] in data:
            m = data[a["id"]]
            proc += [f'{a["name"]}: {m["pct_below_ath"]:.1f} % pod ATH', stats_line(m)]
            prompt = PROMPT_HEAD + "Upozornění: jde jen o TEST notifikací, nic se nekupuje. " \
                     "Shrň stav aktiva.\nData: " + describe(a["name"], m)
            comment = claude_comment(cfg, prompt)
        body = layout("Testovací zpráva hlídače investic", "0 Kč (jen test)", proc, comment)
        send_ntfy(cfg, "Hlídač investic – test", body, tags="white_check_mark")
        return

    month = today.strftime("%Y-%m")
    month_state = state.setdefault(month, {"proposed_czk": 0, "sent": []})
    signals = evaluate(cfg, data, month_state, today)
    if not signals:
        log.info("Nic se nesplnilo – nic neposílám.")
        return

    for sig in signals:
        title, body = build_message(cfg, sig, data)
        log.info("SIGNÁL %s: %s | %s", sig["key"], title, body.replace("\n", " | "))
        if args.dry_run:
            continue
        send_ntfy(cfg, title, body)
        month_state["sent"] = sorted(set(month_state["sent"]) | {sig["key"], *sig.get("also_done", [])})
        if sig["kind"] == "dip" or sig.get("counts"):
            month_state["proposed_czk"] = month_state.get("proposed_czk", 0) + sig["amount"]
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Neočekávaná chyba")
        raise
