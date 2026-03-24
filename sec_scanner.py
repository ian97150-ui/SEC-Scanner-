"""
SEC Filing Scanner
==================
Listens for SEC_CHECK alerts from the Execution-Wall8 app (or any HTTP client),
queries the SEC EDGAR API for today's filings on the requested ticker,
and sends a confirmation via Pushover if a filing is found.

Endpoints:
  POST /sec-check   { "ticker": "AAPL" }           → check today's filings
  POST /sec-check   { "ticker": "AAPL", "date": "2026-03-24" }  → specific date
  GET  /health      → liveness check

Usage:
  python sec_scanner.py
"""

import os
import requests
from flask import Flask, request, jsonify
from datetime import date
import logging

# ================================================================
# CONFIG — fill these in
# ================================================================

PUSHOVER_USER_KEY          = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN         = os.environ.get("PUSHOVER_APP_TOKEN", "")

# Optional: URL on your Execution-Wall8 backend to POST confirmations back to
# Leave empty ("") to skip the callback
EXECUTION_APP_CALLBACK_URL = os.environ.get("EXECUTION_APP_CALLBACK_URL", "")

# Port this scanner listens on
# Railway injects PORT automatically — falls back to 5050 for local use
PORT = int(os.environ.get("PORT", 5050))

# SEC requires a User-Agent identifying your app + contact email
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "SECScanner/1.0 contact@youremail.com")

# Only alert on these filing types — all others are silently ignored
WATCHED_FORM_TYPES = {
    "10-K", "10-K405", "10-KT",
    "10-Q",
    "8-K",
    "F-3", "F-3ASR", "F-3DPOS", "F-3MEF",
    "N-2", "N-2 POSASR",
    "S-1", "S-11", "S-11MEF", "S-1MEF",
    "S-3", "S-3ASR", "S-3D", "S-3DPOS", "S-3MEF",
    "SF-3",
    "6-K",
}

# ================================================================
# SETUP
# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# In-memory cache so we don't reload the ticker→CIK map every request
_ticker_map: dict[str, str] = {}


# ================================================================
# SEC EDGAR HELPERS
# ================================================================

def _sec_headers() -> dict:
    return {"User-Agent": SEC_USER_AGENT}


def load_ticker_map() -> dict[str, str]:
    """
    Download SEC's official ticker → CIK mapping once and cache it.
    Returns { "AAPL": "0000320193", "TSLA": "0001318605", ... }
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    log.info("Loading SEC ticker map from EDGAR...")
    resp = requests.get(url, headers=_sec_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    mapping = {}
    for entry in data.values():
        ticker = entry["ticker"].upper()
        cik = str(entry["cik_str"]).zfill(10)
        mapping[ticker] = cik
    log.info(f"Loaded {len(mapping)} tickers.")
    return mapping


def get_cik(ticker: str) -> str | None:
    """Return zero-padded 10-digit CIK for ticker, or None if not found."""
    global _ticker_map
    if not _ticker_map:
        _ticker_map = load_ticker_map()
    return _ticker_map.get(ticker.upper())


def check_filings(ticker: str, target_date: str | None = None) -> dict:
    """
    Query EDGAR for filings by ticker on target_date (ISO, defaults to today).
    Returns a result dict with keys: found, ticker, date, company_name, filings, error.
    """
    if target_date is None:
        target_date = date.today().isoformat()

    cik = get_cik(ticker)
    if not cik:
        return {
            "found": False,
            "ticker": ticker.upper(),
            "date": target_date,
            "error": f"No CIK found for ticker '{ticker}' — check the symbol."
        }

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=_sec_headers(), timeout=15)

    if resp.status_code != 200:
        return {
            "found": False,
            "ticker": ticker.upper(),
            "date": target_date,
            "error": f"EDGAR returned HTTP {resp.status_code} for CIK {cik}"
        }

    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})

    dates        = recent.get("filingDate", [])
    forms        = recent.get("form", [])
    descriptions = recent.get("primaryDocument", [])
    accessions   = recent.get("accessionNumber", [])

    matched = []
    for i, d in enumerate(dates):
        if d == target_date and forms[i] in WATCHED_FORM_TYPES:
            # Build direct URL to the filing document
            acc_clean = accessions[i].replace("-", "")
            cik_int   = int(cik)
            doc_url   = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{acc_clean}/{descriptions[i]}"
            )
            matched.append({
                "form":       forms[i],
                "date":       d,
                "document":   descriptions[i],
                "accession":  accessions[i],
                "url":        doc_url,
            })

    return {
        "found":        len(matched) > 0,
        "ticker":       ticker.upper(),
        "date":         target_date,
        "company_name": data.get("name", "Unknown"),
        "filings":      matched,
    }


# ================================================================
# NOTIFICATION HELPERS
# ================================================================

def send_pushover(title: str, message: str, url: str = "", url_title: str = "") -> None:
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        log.warning("Pushover keys not configured — skipping notification.")
        return
    payload = {
        "token":   PUSHOVER_APP_TOKEN,
        "user":    PUSHOVER_USER_KEY,
        "title":   title,
        "message": message,
    }
    if url:
        payload["url"]       = url
        payload["url_title"] = url_title or "View Filing"
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            timeout=8
        )
        log.info("Pushover notification sent.")
    except Exception as e:
        log.warning(f"Pushover failed: {e}")


def send_callback(result: dict) -> None:
    """POST the SEC result back to the Execution-Wall8 app if configured."""
    if not EXECUTION_APP_CALLBACK_URL:
        return
    try:
        requests.post(
            EXECUTION_APP_CALLBACK_URL,
            json={"event": "SEC_RESULT", **result},
            timeout=8
        )
        log.info(f"Callback sent to {EXECUTION_APP_CALLBACK_URL}")
    except Exception as e:
        log.warning(f"Callback to Execution app failed: {e}")


# ================================================================
# ROUTES
# ================================================================

@app.route("/sec-check", methods=["POST"])
def sec_check():
    body = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker", "")).strip().upper()
    target_date = body.get("date")  # optional ISO date string
    send_pushover_flag = body.get("send_pushover", True)  # default True if not provided

    if not ticker:
        return jsonify({"error": "ticker field is required"}), 400

    log.info(f"SEC check requested for {ticker} on {target_date or 'today'}")
    result = check_filings(ticker, target_date)

    if result.get("error"):
        log.warning(f"SEC check error: {result['error']}")
        return jsonify(result), 404

    if result["found"]:
        count  = len(result["filings"])
        forms  = ", ".join(f["form"] for f in result["filings"])
        name   = result["company_name"]
        d      = result["date"]
        # Link to the first (most recent) filing
        first_url = result["filings"][0]["url"] if result["filings"] else ""

        log.info(f"✅ CONFIRMED — {ticker} ({name}): {count} filing(s) today [{forms}]")

        if send_pushover_flag:
            send_pushover(
                title    = f"SEC Filing Confirmed: {ticker}",
                message  = f"{name}\n{count} filing(s) on {d}\nForms: {forms}",
                url      = first_url,
                url_title= f"View {result['filings'][0]['form']}"
            )
        send_callback(result)
    else:
        log.info(f"❌ No filings for {ticker} on {result['date']}")

    return jsonify(result), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "date": date.today().isoformat()}), 200


# ================================================================
# MAIN
# ================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  SEC Filing Scanner")
    print(f"  Listening on http://localhost:{PORT}")
    print()
    print("  POST /sec-check  { \"ticker\": \"AAPL\" }")
    print("  GET  /health")
    print("=" * 55)

    # Pre-load the ticker map so first request is fast
    _ticker_map = load_ticker_map()

    app.run(host="0.0.0.0", port=PORT, debug=False)
