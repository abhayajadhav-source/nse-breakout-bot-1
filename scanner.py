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
from typing import List, Dict, Optional

import yfinance as yf
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GMAIL_USER         = os.getenv("GMAIL_USER", "")            # sender@gmail.com
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")    # 16-char app pwd
EMAIL_TO           = os.getenv("EMAIL_TO", "")              # recipient(s), comma-separated
MOCK_RUN           = os.getenv("MOCK_RUN", "false").lower() == "true"

# 0.0   = strictly at/above 52w-high (or at/below 52w-low)
# 0.005 = within 0.5% of the extreme
PROXIMITY = float(os.getenv("PROXIMITY", "0.0"))

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL

# ---------------------------------------------------------------------------
# Nifty 100 universe (Yahoo Finance suffix = .NS)
# Refresh occasionally from
# https://www.niftyindices.com/indices/equity/broad-based-indices/nifty100
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
    "KOTAKBANK", "LTIM", "LT", "LICI", "LODHA", "M&M", "MARUTI", "NTPC",
    "NESTLEIND", "ONGC", "PIDILITIND", "PFC", "POWERGRID", "PNB", "RECLTD",
    "RELIANCE", "SBILIFE", "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "SIEMENS",
    "SBIN", "SUNPHARMA", "SWIGGY", "TVSMOTOR", "TCS", "TATACONSUM", "TATAMOTORS",
    "TATAPOWER", "TATASTEEL", "TECHM", "TITAN", "TORNTPHARM", "TRENT", "ULTRACEMCO",
    "UNITDSPR", "VBL", "VEDL", "WIPRO", "ZYDUSLIFE", "INDUSTOWER",
]

# ---------------------------------------------------------------------------
# Market hours (NSE: 09:15 - 15:30 IST, Mon-Fri)
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
def fetch_ticker_snapshot(symbol: str) -> Optional[Dict]:
    """Return price, volume, 52w high/low for one ticker.

    Strategy:
      1. Try `fast_info` (cheapest call).
      2. If it returns nulls, fall back to a 1-year history pull and
         compute 52w extremes ourselves.
    """
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
            hist = t.history(period="1y", interval="1d", auto_adjust=False)
            if hist is None or hist.empty:
                return None
            last = float(hist["Close"].iloc[-1])
            hi52 = float(hist["High"].max())
            lo52 = float(hist["Low"].min())
            if not vol:
                vol = int(hist["Volume"].iloc[-1])

        if not (last and hi52 and lo52):
            return None

        return {
            "symbol": symbol,
            "last":   last,
            "high52": hi52,
            "low52":  lo52,
            "volume": vol,
            "ticker": t,
        }
    except Exception as e:
        print(f"[WARN] {symbol}: {e}", file=sys.stderr)
        return None


def fetch_top_news(ticker: yf.Ticker, n: int = 2) -> List[Dict]:
    """Return up to n recent news headlines for a ticker."""
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


def scan_universe(symbols: List[str], prox: float) -> List[Dict]:
    hits = []
    for sym in symbols:
        snap = fetch_ticker_snapshot(sym)
        if not snap:
            continue
        kind = classify(snap, prox)
        if kind:
            snap["kind"] = kind
            snap["news"] = fetch_top_news(snap["ticker"])
            hits.append(snap)
        time.sleep(0.05)
    return hits

# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------
def render_html(hits: List[Dict], mock: bool) -> str:
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

    body = ""
    if highs:
        body += f'<h3 style="color:#2ecc71;margin:18px 0 4px;">🚀 52-Week HIGH breakouts ({len(highs)})</h3>'
        body += "".join(row(h) for h in highs)
    if lows:
        body += f'<h3 style="color:#e74c3c;margin:18px 0 4px;">🔻 52-Week LOW breakouts ({len(lows)})</h3>'
        body += "".join(row(h) for h in lows)
    if not hits:
        body = '<p style="color:#888;font-style:italic;">No breakouts detected this cycle.</p>'

    banner = ('<div style="background:#fff3cd;border:1px solid #ffeaa7;'
              'padding:8px 12px;border-radius:4px;margin-bottom:12px;'
              'font-size:13px;">🧪 <b>MOCK RUN</b> — wiring test, not a live alert.</div>'
              if mock else "")

    return f"""\
<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;max-width:720px;margin:0 auto;padding:16px;">
  {banner}
  <h2 style="margin:0 0 4px;">📈 NSE 100 Breakout Alert</h2>
  <div style="color:#888;font-size:13px;margin-bottom:8px;">{now_ist}</div>
  {body}
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0 8px;">
  <div style="color:#aaa;font-size:11px;">
    Sent by NSE Breakout Bot · data: Yahoo Finance · scheduled by GitHub Actions
  </div>
</body></html>"""


def render_text(hits: List[Dict], mock: bool) -> str:
    """Plain-text fallback for clients that block HTML."""
    now_ist = dt.datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M IST")
    lines = []
    if mock:
        lines.append("[MOCK RUN] — wiring test, not a live alert.")
    lines.append(f"NSE 100 Breakout Alert — {now_ist}")
    lines.append("=" * 60)

    if not hits:
        lines.append("No breakouts detected this cycle.")
        return "\n".join(lines)

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
    return "\n".join(lines)


def build_subject(hits: List[Dict], mock: bool) -> str:
    prefix = "[MOCK] " if mock else ""
    if not hits:
        return f"{prefix}NSE 100 Breakout Scan — no hits"
    highs = sum(1 for h in hits if h["kind"] == "HIGH")
    lows  = sum(1 for h in hits if h["kind"] == "LOW")
    parts = []
    if highs: parts.append(f"{highs} 52w-HIGH")
    if lows:  parts.append(f"{lows} 52w-LOW")
    return f"{prefix}NSE 100 Breakout: {', '.join(parts)}"

# ---------------------------------------------------------------------------
# Send via Gmail SMTP
# ---------------------------------------------------------------------------
def send_email(hits: List[Dict], mock: bool) -> bool:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and EMAIL_TO):
        print("[ERROR] GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO not set",
              file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = build_subject(hits, mock)
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO  # comma-separated list works
    msg.set_content(render_text(hits, mock))
    msg.add_alternative(render_html(hits, mock), subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            # Strip any spaces Google may have shown in the app password.
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD.replace(" ", ""))
            s.send_message(msg)
        print(f"Email sent to {EMAIL_TO}")
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

    print(f"Scanning {len(NIFTY_100)} symbols (proximity={PROXIMITY})...")
    hits = scan_universe(NIFTY_100, PROXIMITY)
    print(f"Found {len(hits)} breakouts.")

    # Requirement #7: only email when stocks identified.
    # Mock run is the exception - always sends so you can verify setup.
    if not hits and not MOCK_RUN:
        print("No breakouts -> skipping email (per requirement #7).")
        return 0

    ok = send_email(hits, mock=MOCK_RUN)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
