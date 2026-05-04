"""
NSE 100 - 52-week High/Low Breakout Scanner with Gmail Alerts.

Runs once per invocation. Triggered by GitHub Actions cron every 15 minutes
during NSE market hours.

Free stack:
  - Market data: Yahoo Finance via `yfinance`
  - News:       Yahoo Finance Ticker.news
  - Alerts:     Gmail SMTP (app password)
  - Schedule:   GitHub Actions cron
"""

import os
import sys
import ssl
import time
import smtplib
import datetime as dt
from email.message import EmailMessage
from typing import List, Dict, Optional, Tuple

import yfinance as yf
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_TO           = os.getenv("EMAIL_TO", "")
MOCK_RUN           = os.getenv("MOCK_RUN", "false").lower() == "true"
PROXIMITY = float(os.getenv("PROXIMITY", "0.0"))

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# ---------------------------------------------------------------------------
# Nifty 100 universe
# ---------------------------------------------------------------------------
NIFTY_100 = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER",
    "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "DMART", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BANKBARODA", "BEL", "BPCL",
    "BHARTIARTL", "BOSCHLTD", "BRITANNIA", "CGPOWER", "CANBK", "CHOLAFIN",
    "CIPLA", "COALINDIA", "DLF", "DABUR", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "ETERNAL", "GAIL", "GODREJCP", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HAVELLS", "HEROMOTOCO", "HINDALCO", "HAL", "HINDUNILVR", "HYUNDAI", "ICICIBANK",
    "ICICIGI", "ICICIPRULI", "ITC", "INDHOTEL", "IOC", "IRFC", "INDUSINDBK",
    "NAUKRI", "INFY", "INDIGO", "JSWENERGY", "JSWSTEEL", "JINDALSTEL", "JIOFIN",
    "KOTAKBANK", "LTM", "LT", "LICI", "LODHA", "M&M", "MARUTI", "NTPC",
    "NESTLEIND", "ONGC", "PIDILITIND", "PFC", "POWERGRID", "PNB", "RECLTD",
    "RELIANCE", "SBILIFE", "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "SIEMENS",
    "SBIN", "SUNPHARMA", "SWIGGY", "TVSMOTOR", "TCS", "TATACONSUM", "TMPV",
    "TATAPOWER", "TATASTEEL", "TECHM", "TITAN", "TORNTPHARM", "TRENT", "ULTRACEMCO",
    "UNITDSPR", "VBL", "VEDL", "WIPRO", "ZYDUSLIFE", "INDUSTOWER",
]

# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
def is_market_open(now: Optional[dt.datetime] = None) -> bool:
    now = now or dt.datetime.now(tz=IST)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t

# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------
DELISTED_HINTS = ("possibly delisted", "no price data found",
                  "Quote not found", "No data found")

def fetch_ticker_snapshot(symbol: str) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        t = yf.Ticker(f"{symbol}.NS")
        last = hi52 = lo52 = 0.0
        vol  = 0

        try:
            fi = t.fast_info
            last = float(fi.get("last_price") or 0)
            hi52 = float(fi.get("year_high")  or 0)
            lo52 = float(fi.get("year_low")   or 0)
            vol  = int(fi.get("last_volume")  or 0)
        except Exception:
            pass

        if not (last and hi52 and lo52):
            try:
                hist = t.history(period="1y", interval="1d", auto_adjust=False)
            except Exception as e:
                msg = str(e)
                if any(h in msg for h in DELISTED_HINTS):
                    return None, "DEAD"
                return None, "TRANSIENT"

            if hist is None or hist.empty:
                return None, "DEAD"

            last = float(hist["Close"].iloc[-1])
            hi52 = float(hist["High"].max())
            lo52 = float(hist["Low"].min())
            if not vol:
                vol = int(hist["Volume"].iloc[-1])

        if not (last and hi52 and lo52):
            return None, "DEAD"

        return {
            "symbol": symbol, "last": last, "high52": hi52,
            "low52": lo52, "volume": vol, "ticker": t,
        }, None

    except Exception as e:
        msg = str(e)
        kind = "DEAD" if any(h in msg for h in DELISTED_HINTS) else "TRANSIENT"
        print(f"[WARN] {symbol}: {kind} - {e}", file=sys.stderr)
        return None, kind


def fetch_top_news(ticker: yf.Ticker, n: int = 2) -> List[Dict]:
    try:
        items = ticker.news or []
        out = []
        for it in items[:n]:
            content = it.get("content", it)
            title   = content.get("title") or it.get("title", "")
            url     = (content.get("canonicalUrl", {}) or {}).get("url") \
                      or content.get("clickThroughUrl", {}).get("url") \
                      or it.get("link", "")
            if title:
                out.append({"title": title, "url": url})
        return out
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Breakout detection
# ---------------------------------------------------------------------------
def classify(snap: Dict, prox: float) -> Optional[str]:
    last, hi, lo = snap["last"], snap["high52"], snap["low52"]
    if last >= hi * (1 - prox):
        return "HIGH"
    if last <= lo * (1 + prox):
        return "LOW"
    return None


def scan_universe(symbols: List[str], prox: float
                  ) -> Tuple[List[Dict], List[str], List[str]]:
    hits, dead, transient = [], [], []
    for sym in symbols:
        snap, err = fetch_ticker_snapshot(sym)
        if snap is None:
            (dead if err == "DEAD" else transient).append(sym)
            continue
        kind = classify(snap, prox)
        if kind:
            snap["kind"] = kind
            snap["news"] = fetch_top_news(snap["ticker"])
            hits.append(snap)
        time.sleep(0.05)
    return hits, dead, transient

# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------
def render_html(hits: List[Dict], mock: bool, dead: List[str],
                transient_count: int, scanned: int) -> str:                  # CHANGED signature
    now_ist = dt.datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M IST")
    highs = [h for h in hits if h["kind"] == "HIGH"]
    lows  = [h for h in hits if h["kind"] == "LOW"]

    def row(h: Dict) -> str:
        is_high = h["kind"] == "HIGH"
        color   = "#2ecc71" if is_high else "#e74c3c"
        emoji   = "🚀" if is_high else "🔻"
        news_html = ""
        for n in h["news"]:
            title = n["title"][:140]
            if n["url"]:
                news_html += f'<li><a href="{n["url"]}" style="color:#1a73e8;text-decoration:none;">{title}</a></li>'
            else:
                news_html += f"<li>{title}</li>"
        if not news_html:
            news_html = '<li style="color:#888;"><i>No recent headlines.</i></li>'

        return f"""
        <div style="border-left:4px solid {color};padding:12px 16px;margin:12px 0;background:#fafafa;border-radius:4px;">
          <div style="font-size:16px;font-weight:600;margin-bottom:6px;">
            {emoji}
            <a href="https://finance.yahoo.com/quote/{h['symbol']}.NS"
               style="color:#222;text-decoration:none;">{h['symbol']}</a>
            <span style="color:{color};font-size:13px;">· 52-Week {'HIGH' if is_high else 'LOW'}</span>
          </div>
          <table style="font-size:13px;color:#444;border-collapse:collapse;">
            <tr>
              <td style="padding:2px 12px 2px 0;"><b>Last</b></td>
              <td style="padding:2px 24px 2px 0;">₹{h['last']:.2f}</td>
              <td style="padding:2px 12px 2px 0;"><b>Volume</b></td>
              <td style="padding:2px 24px 2px 0;">{h['volume']:,}</td>
              <td style="padding:2px 12px 2px 0;"><b>52w Range</b></td>
              <td style="padding:2px 0;">₹{h['low52']:.2f} – ₹{h['high52']:.2f}</td>
            </tr>
          </table>
          <div style="margin-top:8px;font-size:13px;">
            <b>📰 News</b>
            <ul style="margin:4px 0 0 18px;padding:0;">{news_html}</ul>
          </div>
        </div>
        """

    # NEW: heartbeat layout when no hits — small, scannable, distinguishable.
    if not hits:
        body = f"""
        <div style="background:#eef5ff;border:1px solid #cfe0fb;border-radius:6px;
                    padding:14px 18px;margin:8px 0;font-size:14px;color:#1f3b6e;">
          ✅ <b>Heartbeat — pipeline is alive.</b><br>
          Scanned {scanned} symbols, found <b>0</b> breakouts at 52-week extremes
          (proximity = {PROXIMITY}).
          <div style="color:#5a7099;margin-top:6px;font-size:12px;">
            This message confirms GitHub Actions is firing on schedule.
            No action needed.
          </div>
        </div>
        """
    else:
        body = ""
        if highs:
            body += f'<h3 style="color:#2ecc71;margin:18px 0 4px;">🚀 52-Week HIGH breakouts ({len(highs)})</h3>'
            body += "".join(row(h) for h in highs)
        if lows:
            body += f'<h3 style="color:#e74c3c;margin:18px 0 4px;">🔻 52-Week LOW breakouts ({len(lows)})</h3>'
            body += "".join(row(h) for h in lows)

    dead_block = ""
    if dead:
        dead_block = (
            '<div style="background:#fff3cd;border:1px solid #ffeaa7;'
            'padding:8px 12px;border-radius:4px;margin-top:16px;font-size:12px;color:#856404;">'
            f'⚠️ <b>Stale tickers</b> ({len(dead)}): ' + ", ".join(dead) +
            '. These returned 404 / no data — likely renamed or delisted. '
            'Update <code>NIFTY_100</code> in scanner.py.</div>'
        )

    transient_block = ""
    if transient_count:
        transient_block = (
            '<div style="background:#f4f4f4;border:1px solid #ddd;'
            'padding:6px 10px;border-radius:4px;margin-top:8px;font-size:11px;color:#666;">'
            f'ℹ️ {transient_count} transient fetch errors (will retry next cycle).</div>'
        )

    banner = ('<div style="background:#fff3cd;border:1px solid #ffeaa7;'
              'padding:8px 12px;border-radius:4px;margin-bottom:12px;'
              'font-size:13px;">🧪 <b>MOCK RUN</b> — wiring test, not a live alert.</div>'
              if mock else "")

    title = "📈 NSE 100 Breakout Alert" if hits else "💓 NSE 100 Scan — Heartbeat"

    return f"""\
<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;max-width:720px;margin:0 auto;padding:16px;">
  {banner}
  <h2 style="margin:0 0 4px;">{title}</h2>
  <div style="color:#888;font-size:13px;margin-bottom:8px;">{now_ist}</div>
  {body}
  {dead_block}
  {transient_block}
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0 8px;">
  <div style="color:#aaa;font-size:11px;">
    Sent by NSE Breakout Bot · data: Yahoo Finance · scheduled by GitHub Actions
  </div>
</body></html>"""


def render_text(hits: List[Dict], mock: bool, dead: List[str],
                transient_count: int, scanned: int) -> str:                  # CHANGED signature
    now_ist = dt.datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M IST")
    lines = []
    if mock:
        lines.append("[MOCK RUN] — wiring test, not a live alert.")

    if not hits:
        lines.append(f"NSE 100 Scan — Heartbeat — {now_ist}")
        lines.append("=" * 60)
        lines.append(f"Pipeline alive. Scanned {scanned} symbols, 0 breakouts.")
        lines.append("(GitHub Actions cron is firing on schedule.)")
    else:
        lines.append(f"NSE 100 Breakout Alert — {now_ist}")
        lines.append("=" * 60)
        for kind, label in [("HIGH", "52-Week HIGH"), ("LOW", "52-Week LOW")]:
            items = [h for h in hits if h["kind"] == kind]
            if not items:
                continue
            lines.append("")
            lines.append(f"{label} breakouts ({len(items)}):")
            for h in items:
                lines.append(
                    f"  {h['symbol']:<14} ₹{h['last']:>10.2f}  "
                    f"vol={h['volume']:>12,}  52w=[{h['low52']:.2f}–{h['high52']:.2f}]"
                )
                for n in h["news"]:
                    lines.append(f"    - {n['title'][:120]}")
                    if n["url"]:
                        lines.append(f"      {n['url']}")

    if dead:
        lines.append("")
        lines.append(f"[!] Stale tickers ({len(dead)}): " + ", ".join(dead))
    if transient_count:
        lines.append(f"[i] {transient_count} transient errors (retry next cycle).")
    return "\n".join(lines)


# CHANGED: subject lines now make heartbeat vs alert obvious for filtering.
def build_subject(hits: List[Dict], mock: bool) -> str:
    prefix = "[MOCK] " if mock else ""
    if not hits:
        # Heartbeat tag — easy to filter / auto-archive.
        return f"{prefix}[NSE-Heartbeat] Pipeline alive — no breakouts"
    highs = sum(1 for h in hits if h["kind"] == "HIGH")
    lows  = sum(1 for h in hits if h["kind"] == "LOW")
    parts = []
    if highs: parts.append(f"{highs} 52w-HIGH")
    if lows:  parts.append(f"{lows} 52w-LOW")
    return f"{prefix}[NSE-Alert] {', '.join(parts)}"

# ---------------------------------------------------------------------------
# Send via Gmail SMTP
# ---------------------------------------------------------------------------
def send_email(hits: List[Dict], mock: bool, dead: List[str],
               transient_count: int, scanned: int) -> bool:                  # CHANGED signature
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and EMAIL_TO):
        print("[ERROR] GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO not set",
              file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = build_subject(hits, mock)
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.set_content(render_text(hits, mock, dead, transient_count, scanned))
    msg.add_alternative(render_html(hits, mock, dead, transient_count, scanned),
                        subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD.replace(" ", ""))
            s.send_message(msg)
        kind = "alert" if hits else "heartbeat"
        print(f"Email ({kind}) sent to {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"[ERROR] SMTP: {e}", file=sys.stderr)
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not MOCK_RUN and not is_market_open():
        print("Market closed - exiting without scan.")
        return 0

    scanned = len(NIFTY_100)
    print(f"Scanning {scanned} symbols (proximity={PROXIMITY})...")
    hits, dead, transient = scan_universe(NIFTY_100, PROXIMITY)

    print("=" * 50)
    print(f"Breakouts:        {len(hits)}")
    print(f"Stale (404/dead): {len(dead)}  {dead if dead else ''}")
    print(f"Transient errors: {len(transient)}  {transient if transient else ''}")
    print("=" * 50)

    # CHANGED: always send. Heartbeat when 0 hits, full alert when there are.
    ok = send_email(hits, mock=MOCK_RUN, dead=dead,
                    transient_count=len(transient), scanned=scanned)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
