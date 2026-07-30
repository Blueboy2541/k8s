from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from flask import Flask, redirect, render_template_string, request, url_for
from prometheus_client import Gauge, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fang-stock-metrics")

DEFAULT_TICKERS = ["AMZN", "GOOGL", "META", "NFLX", "ORCL"]
TICKERS_FILE = Path(os.environ.get("TICKERS_FILE", "tickers.json"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8000"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8080"))
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"

# Ticker symbols: uppercase, may contain digits, dots or dashes (e.g. BRK.B, RDS-A).
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

stock_price = Gauge(
    "fang_stock_price_usd",
    "Latest price in USD for a tracked stock ticker",
    ["ticker"],
)
scrape_errors = Gauge(
    "fang_stock_scrape_errors_total",
    "Number of consecutive failed price fetches for a ticker",
    ["ticker"],
)
tracked_tickers = Gauge(
    "fang_tracked_tickers",
    "Number of tickers currently in the polling list",
)

# The ticker list and last-seen prices are read by the Flask thread and written by
# the polling loop, so every access goes through this lock.
_state_lock = threading.RLock()
_tickers: list[str] = []
_last_prices: dict[str, float] = {}


def normalize(symbol: str) -> str:
    """Canonical form of a ticker symbol: trimmed and uppercased."""
    return symbol.strip().upper()


def _read_tickers_file() -> list[str] | None:
    """Read the ticker list from disk. Returns None if it is missing or unusable."""
    try:
        raw = json.loads(TICKERS_FILE.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        log.exception("could not read %s, falling back to defaults", TICKERS_FILE)
        return None

    if not isinstance(raw, list):
        log.error("%s does not contain a JSON list, ignoring it", TICKERS_FILE)
        return None

    return [s for s in (normalize(str(item)) for item in raw) if TICKER_RE.match(s)]


def _write_tickers_file(tickers: list[str]) -> None:
    """Persist the ticker list, writing to a temp file first so it is never half-written."""
    TICKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TICKERS_FILE.with_name(TICKERS_FILE.name + ".tmp")
    tmp_path.write_text(json.dumps(tickers, indent=2) + "\n")
    os.replace(tmp_path, TICKERS_FILE)


def load_tickers() -> None:
    """Populate the in-memory list at startup, seeding the file on first run."""
    global _tickers
    stored = _read_tickers_file()
    with _state_lock:
        if stored is None:
            _tickers = list(DEFAULT_TICKERS)
            _write_tickers_file(_tickers)
            log.info("seeded %s with defaults: %s", TICKERS_FILE, ", ".join(_tickers))
        else:
            _tickers = stored
            log.info("loaded %d ticker(s) from %s", len(_tickers), TICKERS_FILE)
        tracked_tickers.set(len(_tickers))


def get_tickers() -> list[str]:
    """Snapshot of the current ticker list, safe to iterate outside the lock."""
    with _state_lock:
        return list(_tickers)


def add_ticker(raw: str) -> str:
    """Add a ticker to the tracked list. Returns a message for the web UI."""
    symbol = normalize(raw)
    if not TICKER_RE.match(symbol):
        return f"'{raw.strip()}' is not a valid ticker symbol."

    with _state_lock:
        if symbol in _tickers:
            return f"{symbol} is already tracked."
        _tickers.append(symbol)
        _tickers.sort()
        _write_tickers_file(_tickers)
        tracked_tickers.set(len(_tickers))

    log.info("added ticker %s", symbol)
    return f"Added {symbol} - it will be polled on the next cycle."


def remove_ticker(raw: str) -> str:
    """Remove a ticker and drop its exported metrics. Returns a message for the web UI."""
    symbol = normalize(raw)

    with _state_lock:
        if symbol not in _tickers:
            return f"{symbol} is not tracked."
        _tickers.remove(symbol)
        _last_prices.pop(symbol, None)
        _write_tickers_file(_tickers)
        tracked_tickers.set(len(_tickers))

    # Stop exporting the labelled series so Prometheus sees the ticker disappear.
    for metric in (stock_price, scrape_errors):
        try:
            metric.remove(symbol)
        except KeyError:
            pass

    log.info("removed ticker %s", symbol)
    return f"Removed {symbol}."


def poll_once() -> None:
    for ticker in get_tickers():
        try:
            response = requests.get(
                FINNHUB_QUOTE_URL,
                params={"symbol": ticker, "token": FINNHUB_API_KEY},
                timeout=10,
            )
            response.raise_for_status()
            price = float(response.json()["c"])
            stock_price.labels(ticker=ticker).set(price)
            scrape_errors.labels(ticker=ticker).set(0)
            with _state_lock:
                _last_prices[ticker] = price
            log.info("ticker=%s price=%.2f", ticker, price)
        except Exception:
            scrape_errors.labels(ticker=ticker).inc()
            log.exception("failed to fetch price for ticker=%s", ticker)


admin = Flask(__name__)

PAGE = """<!doctype html>
<title>Tracked tickers</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  p.sub { color: #888; margin-top: 0; font-size: 0.9rem; }
  .msg { padding: 0.6rem 0.8rem; border-left: 3px solid #6b8afd; background: #6b8afd1a;
         border-radius: 3px; margin-bottom: 1.5rem; font-size: 0.9rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { text-align: left; padding: 0.5rem 0.4rem; border-bottom: 1px solid #8884; }
  th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #888; }
  td.price { font-variant-numeric: tabular-nums; }
  td.actions { text-align: right; }
  button { font: inherit; cursor: pointer; border-radius: 4px; border: 1px solid #8886;
           background: transparent; color: inherit; padding: 0.25rem 0.6rem; }
  button.primary { background: #6b8afd; border-color: #6b8afd; color: #fff; padding: 0.4rem 1rem; }
  input { font: inherit; padding: 0.4rem 0.5rem; border-radius: 4px; border: 1px solid #8886;
          background: transparent; color: inherit; width: 10rem; }
  form.add { display: flex; gap: 0.5rem; }
  footer { margin-top: 2rem; font-size: 0.85rem; color: #888; }
</style>

<h1>Tracked tickers</h1>
<p class="sub">Polling every {{ interval }}s &middot; stored in {{ store }}</p>

{% if message %}<div class="msg">{{ message }}</div>{% endif %}

<table>
  <tr><th>Ticker</th><th>Last price</th><th></th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.ticker }}</td>
    <td class="price">{% if row.price is none %}&mdash;{% else %}${{ '%.2f'|format(row.price) }}{% endif %}</td>
    <td class="actions">
      <form method="post" action="{{ url_for('remove') }}">
        <input type="hidden" name="ticker" value="{{ row.ticker }}">
        <button type="submit">Remove</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}
  <tr><td colspan="3" style="color:#888">No tickers tracked yet.</td></tr>
  {% endif %}
</table>

<form class="add" method="post" action="{{ url_for('add') }}">
  <input name="ticker" placeholder="e.g. AAPL" autofocus>
  <button class="primary" type="submit">Add ticker</button>
</form>

<footer>Prometheus metrics are served separately on port {{ metrics_port }} at <code>/metrics</code>.</footer>
"""


@admin.get("/")
def index():
    with _state_lock:
        rows = [{"ticker": t, "price": _last_prices.get(t)} for t in _tickers]
    return render_template_string(
        PAGE,
        rows=rows,
        message=request.args.get("msg"),
        interval=POLL_INTERVAL_SECONDS,
        store=TICKERS_FILE,
        metrics_port=METRICS_PORT,
    )


@admin.post("/add")
def add():
    message = add_ticker(request.form.get("ticker", ""))
    return redirect(url_for("index", msg=message))


@admin.post("/remove")
def remove():
    message = remove_ticker(request.form.get("ticker", ""))
    return redirect(url_for("index", msg=message))


def main() -> None:
    load_tickers()
    start_http_server(METRICS_PORT)
    threading.Thread(
        target=lambda: admin.run(host="0.0.0.0", port=ADMIN_PORT, threaded=True),
        daemon=True,
    ).start()
    log.info(
        "metrics on :%d/metrics, ticker admin UI on :%d, polling every %ds",
        METRICS_PORT,
        ADMIN_PORT,
        POLL_INTERVAL_SECONDS,
    )
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
