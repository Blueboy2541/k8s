from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from flask import Flask, redirect, render_template_string, request, url_for
from prometheus_client import Gauge, start_http_server
from werkzeug.middleware.proxy_fix import ProxyFix

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


@dataclass
class Holding:
    """A tracked ticker, optionally with a position attached.

    quantity of 0 means "watchlist only" - price is still polled, but no position
    or portfolio metrics are exported for it.
    """

    symbol: str
    quantity: float = 0.0
    cost_basis: float = 0.0  # average price paid per share


# --- Quote metrics: one call to /quote fills all of these ------------------------

stock_price = Gauge(
    "fang_stock_price_usd",
    "Latest price in USD for a tracked stock ticker",
    ["ticker"],
)
stock_change = Gauge(
    "fang_stock_change_usd",
    "Change in USD since the previous close",
    ["ticker"],
)
stock_change_percent = Gauge(
    "fang_stock_change_percent",
    "Percent change since the previous close",
    ["ticker"],
)
stock_day_high = Gauge(
    "fang_stock_day_high_usd",
    "Highest price so far in the current trading day",
    ["ticker"],
)
stock_day_low = Gauge(
    "fang_stock_day_low_usd",
    "Lowest price so far in the current trading day",
    ["ticker"],
)
stock_open = Gauge(
    "fang_stock_open_usd",
    "Opening price for the current trading day",
    ["ticker"],
)
stock_previous_close = Gauge(
    "fang_stock_previous_close_usd",
    "Closing price of the previous trading day",
    ["ticker"],
)

# --- Position metrics: only exported for holdings with quantity > 0 --------------

position_quantity = Gauge(
    "fang_position_quantity",
    "Number of shares held",
    ["ticker"],
)
position_value = Gauge(
    "fang_position_value_usd",
    "Current market value of the position",
    ["ticker"],
)
position_cost = Gauge(
    "fang_position_cost_usd",
    "Total amount paid for the position",
    ["ticker"],
)
position_pnl = Gauge(
    "fang_position_pnl_usd",
    "Unrealised profit or loss on the position in USD",
    ["ticker"],
)
position_pnl_percent = Gauge(
    "fang_position_pnl_percent",
    "Unrealised profit or loss on the position as a percentage of cost",
    ["ticker"],
)

# --- Portfolio and health metrics -----------------------------------------------

portfolio_value = Gauge("fang_portfolio_value_usd", "Market value of all positions")
portfolio_cost = Gauge("fang_portfolio_cost_usd", "Total amount paid for all positions")
portfolio_pnl = Gauge("fang_portfolio_pnl_usd", "Unrealised profit or loss across all positions")
scrape_errors = Gauge(
    "fang_stock_scrape_errors_total",
    "Number of consecutive failed price fetches for a ticker",
    ["ticker"],
)
tracked_tickers = Gauge(
    "fang_tracked_tickers",
    "Number of tickers currently in the polling list",
)

# Grouped so a removed ticker can be cleared from every series in one loop.
QUOTE_METRICS = (
    stock_price,
    stock_change,
    stock_change_percent,
    stock_day_high,
    stock_day_low,
    stock_open,
    stock_previous_close,
    scrape_errors,
)
POSITION_METRICS = (
    position_quantity,
    position_value,
    position_cost,
    position_pnl,
    position_pnl_percent,
)

# Holdings and last-seen quotes are read by the Flask thread and written by the
# polling loop, so every access goes through this lock.
_state_lock = threading.RLock()
_holdings: list[Holding] = []
_last_quotes: dict[str, dict[str, float]] = {}


def normalize(symbol: str) -> str:
    """Canonical form of a ticker symbol: trimmed and uppercased."""
    return symbol.strip().upper()


def _num(value: object) -> float:
    """Finnhub returns null for some fields outside market hours."""
    return float(value) if isinstance(value, (int, float)) else 0.0


def _parse_amount(raw: str, field: str) -> float:
    """Parse a non-negative number from a form field. Blank means zero."""
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{field} must be a number.")
    if value < 0:
        raise ValueError(f"{field} cannot be negative.")
    return value


def _parse_holding(item: object) -> Holding | None:
    """Accept either a bare symbol string (the original format) or a holding dict."""
    if isinstance(item, str):
        symbol = normalize(item)
        return Holding(symbol) if TICKER_RE.match(symbol) else None
    if isinstance(item, dict):
        symbol = normalize(str(item.get("symbol", "")))
        if not TICKER_RE.match(symbol):
            return None
        return Holding(symbol, _num(item.get("quantity")), _num(item.get("cost_basis")))
    return None


def _read_holdings_file() -> list[Holding] | None:
    """Read holdings from disk. Returns None if the file is missing or unusable."""
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

    return [h for h in (_parse_holding(item) for item in raw) if h is not None]


def _write_holdings_file(holdings: list[Holding]) -> None:
    """Persist holdings, writing to a temp file first so it is never half-written."""
    TICKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"symbol": h.symbol, "quantity": h.quantity, "cost_basis": h.cost_basis}
        for h in holdings
    ]
    tmp_path = TICKERS_FILE.with_name(TICKERS_FILE.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp_path, TICKERS_FILE)


def load_holdings() -> None:
    """Populate the in-memory list at startup, seeding the file on first run."""
    global _holdings
    stored = _read_holdings_file()
    with _state_lock:
        if stored is None:
            _holdings = [Holding(s) for s in DEFAULT_TICKERS]
            _write_holdings_file(_holdings)
            log.info("seeded %s with defaults: %s", TICKERS_FILE, ", ".join(DEFAULT_TICKERS))
        else:
            _holdings = stored
            log.info("loaded %d holding(s) from %s", len(_holdings), TICKERS_FILE)
        tracked_tickers.set(len(_holdings))


def get_holdings() -> list[Holding]:
    """Snapshot of current holdings, safe to iterate outside the lock."""
    with _state_lock:
        return [Holding(h.symbol, h.quantity, h.cost_basis) for h in _holdings]


def _find(symbol: str) -> Holding | None:
    return next((h for h in _holdings if h.symbol == symbol), None)


def _clear_position_metrics(symbol: str) -> None:
    for metric in POSITION_METRICS:
        try:
            metric.remove(symbol)
        except KeyError:
            pass


def add_holding(raw_symbol: str, raw_quantity: str, raw_cost: str) -> str:
    """Add a ticker, optionally with a position. Returns a message for the web UI."""
    symbol = normalize(raw_symbol)
    if not TICKER_RE.match(symbol):
        return f"'{raw_symbol.strip()}' is not a valid ticker symbol."
    try:
        quantity = _parse_amount(raw_quantity, "Quantity")
        cost_basis = _parse_amount(raw_cost, "Cost basis")
    except ValueError as exc:
        return str(exc)

    with _state_lock:
        if _find(symbol) is not None:
            return f"{symbol} is already tracked."
        _holdings.append(Holding(symbol, quantity, cost_basis))
        _holdings.sort(key=lambda h: h.symbol)
        _write_holdings_file(_holdings)
        tracked_tickers.set(len(_holdings))

    log.info("added %s quantity=%s cost_basis=%s", symbol, quantity, cost_basis)
    return f"Added {symbol} - it will be polled on the next cycle."


def update_holding(raw_symbol: str, raw_quantity: str, raw_cost: str) -> str:
    """Change the position attached to an existing ticker."""
    symbol = normalize(raw_symbol)
    try:
        quantity = _parse_amount(raw_quantity, "Quantity")
        cost_basis = _parse_amount(raw_cost, "Cost basis")
    except ValueError as exc:
        return str(exc)

    with _state_lock:
        holding = _find(symbol)
        if holding is None:
            return f"{symbol} is not tracked."
        holding.quantity = quantity
        holding.cost_basis = cost_basis
        _write_holdings_file(_holdings)

    # Dropping to zero shares means the position series should disappear entirely
    # rather than linger at 0.
    if quantity == 0:
        _clear_position_metrics(symbol)

    log.info("updated %s quantity=%s cost_basis=%s", symbol, quantity, cost_basis)
    return f"Updated {symbol}."


def remove_holding(raw_symbol: str) -> str:
    """Remove a ticker and drop its exported metrics."""
    symbol = normalize(raw_symbol)

    with _state_lock:
        holding = _find(symbol)
        if holding is None:
            return f"{symbol} is not tracked."
        _holdings.remove(holding)
        _last_quotes.pop(symbol, None)
        _write_holdings_file(_holdings)
        tracked_tickers.set(len(_holdings))

    # Stop exporting the labelled series so Prometheus sees the ticker disappear.
    for metric in QUOTE_METRICS:
        try:
            metric.remove(symbol)
        except KeyError:
            pass
    _clear_position_metrics(symbol)

    log.info("removed ticker %s", symbol)
    return f"Removed {symbol}."


def fetch_quote(ticker: str) -> dict[str, float]:
    """Fetch one quote from Finnhub. Raises on transport, HTTP or data errors."""
    response = requests.get(
        FINNHUB_QUOTE_URL,
        params={"symbol": ticker, "token": FINNHUB_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = _num(payload.get("c"))
    if price == 0:
        # Finnhub answers 200 with an all-zero body for symbols it does not know,
        # so treat that as a failure instead of publishing a $0.00 price.
        raise ValueError(f"no quote data for {ticker} (unknown symbol?)")
    return {
        "price": price,
        "change": _num(payload.get("d")),
        "change_percent": _num(payload.get("dp")),
        "high": _num(payload.get("h")),
        "low": _num(payload.get("l")),
        "open": _num(payload.get("o")),
        "previous_close": _num(payload.get("pc")),
    }


def poll_once() -> None:
    total_value = 0.0
    total_cost = 0.0

    for holding in get_holdings():
        ticker = holding.symbol
        quote: dict[str, float] | None = None

        try:
            quote = fetch_quote(ticker)
            stock_price.labels(ticker=ticker).set(quote["price"])
            stock_change.labels(ticker=ticker).set(quote["change"])
            stock_change_percent.labels(ticker=ticker).set(quote["change_percent"])
            stock_day_high.labels(ticker=ticker).set(quote["high"])
            stock_day_low.labels(ticker=ticker).set(quote["low"])
            stock_open.labels(ticker=ticker).set(quote["open"])
            stock_previous_close.labels(ticker=ticker).set(quote["previous_close"])
            scrape_errors.labels(ticker=ticker).set(0)
            with _state_lock:
                _last_quotes[ticker] = quote
            log.info(
                "ticker=%s price=%.2f change=%.2f%%", ticker, quote["price"], quote["change_percent"]
            )
        except Exception:
            scrape_errors.labels(ticker=ticker).inc()
            log.exception("failed to fetch quote for ticker=%s", ticker)
            # Value the position at the last good price rather than dropping it out
            # of the portfolio total for one bad poll.
            with _state_lock:
                quote = _last_quotes.get(ticker)

        if quote is None or holding.quantity <= 0:
            continue

        value = quote["price"] * holding.quantity
        cost = holding.cost_basis * holding.quantity
        position_quantity.labels(ticker=ticker).set(holding.quantity)
        position_value.labels(ticker=ticker).set(value)
        position_cost.labels(ticker=ticker).set(cost)
        position_pnl.labels(ticker=ticker).set(value - cost)
        if cost > 0:
            position_pnl_percent.labels(ticker=ticker).set((value - cost) / cost * 100)
        total_value += value
        total_cost += cost

    portfolio_value.set(total_value)
    portfolio_cost.set(total_cost)
    portfolio_pnl.set(total_value - total_cost)


admin = Flask(__name__)

# Behind the ingress the app is mounted at /stocks, but nginx strips that prefix
# before forwarding. ProxyFix reads the X-Forwarded-Prefix header the ingress sets
# so url_for() still emits /stocks/add rather than /add. With no proxy in front
# (direct NodePort access) the headers are absent and the app serves at / as normal.
admin.wsgi_app = ProxyFix(admin.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

PAGE = """<!doctype html>
<title>Portfolio</title>
<style>
  :root { color-scheme: light dark; --up: #16a34a; --down: #dc2626; }
  body { font-family: system-ui, sans-serif; max-width: 62rem; margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  p.sub { color: #888; margin-top: 0; font-size: 0.9rem; }
  .msg { padding: 0.6rem 0.8rem; border-left: 3px solid #6b8afd; background: #6b8afd1a;
         border-radius: 3px; margin-bottom: 1.5rem; font-size: 0.9rem; }
  .totals { display: flex; gap: 2.5rem; margin: 1.5rem 0 2rem; flex-wrap: wrap; }
  .totals div span { display: block; font-size: 0.7rem; text-transform: uppercase;
                     letter-spacing: 0.06em; color: #888; margin-bottom: 0.2rem; }
  .totals div strong { font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  th, td { text-align: right; padding: 0.5rem 0.4rem; border-bottom: 1px solid #8884;
           font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  th { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: #888;
       font-weight: 600; }
  td.sym { font-weight: 600; }
  .up { color: var(--up); }
  .down { color: var(--down); }
  .muted { color: #888; }
  button { font: inherit; cursor: pointer; border-radius: 4px; border: 1px solid #8886;
           background: transparent; color: inherit; padding: 0.25rem 0.6rem; }
  button.primary { background: #6b8afd; border-color: #6b8afd; color: #fff; padding: 0.4rem 1rem; }
  input { font: inherit; padding: 0.3rem 0.4rem; border-radius: 4px; border: 1px solid #8886;
          background: transparent; color: inherit; width: 6rem; text-align: right; }
  input.sym { width: 7rem; text-align: left; }
  form.row { display: flex; gap: 0.3rem; justify-content: flex-end; }
  form.add { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;
             padding-top: 0.5rem; }
  form.add label { font-size: 0.75rem; color: #888; display: flex; flex-direction: column;
                   gap: 0.2rem; }
  footer { margin-top: 2rem; font-size: 0.85rem; color: #888; }
  .scroll { overflow-x: auto; }
</style>

<h1>Portfolio</h1>
<p class="sub">Polling every {{ interval }}s &middot; stored in {{ store }}</p>

{% if message %}<div class="msg">{{ message }}</div>{% endif %}

<div class="totals">
  <div><span>Market value</span><strong>${{ '%.2f'|format(total_value) }}</strong></div>
  <div><span>Cost</span><strong>${{ '%.2f'|format(total_cost) }}</strong></div>
  <div><span>Unrealised P&amp;L</span>
    <strong class="{{ 'up' if total_pnl >= 0 else 'down' }}">
      {{ '+' if total_pnl >= 0 else '' }}${{ '%.2f'|format(total_pnl) }}
    </strong>
  </div>
</div>

<div class="scroll">
<table>
  <tr>
    <th>Ticker</th><th>Price</th><th>Day</th><th>Range</th>
    <th>Qty</th><th>Avg cost</th><th>Value</th><th>P&amp;L</th><th></th>
  </tr>
  {% for row in rows %}
  <tr>
    <td class="sym">{{ row.symbol }}</td>
    <td>{% if row.price is none %}<span class="muted">&mdash;</span>
        {% else %}${{ '%.2f'|format(row.price) }}{% endif %}</td>
    <td class="{{ 'up' if row.change_percent >= 0 else 'down' }}">
      {% if row.price is none %}<span class="muted">&mdash;</span>
      {% else %}{{ '+' if row.change_percent >= 0 else '' }}{{ '%.2f'|format(row.change_percent) }}%{% endif %}
    </td>
    <td class="muted">{% if row.price is none %}&mdash;
        {% else %}{{ '%.2f'|format(row.low) }} &ndash; {{ '%.2f'|format(row.high) }}{% endif %}</td>
    <td><input form="edit-{{ row.symbol }}" name="quantity"
               value="{{ '%g'|format(row.quantity) }}"></td>
    <td><input form="edit-{{ row.symbol }}" name="cost_basis"
               value="{{ '%g'|format(row.cost_basis) }}"></td>
    <td>{% if row.value is none %}<span class="muted">&mdash;</span>
        {% else %}${{ '%.2f'|format(row.value) }}{% endif %}</td>
    <td class="{{ 'up' if (row.pnl or 0) >= 0 else 'down' }}">
      {% if row.pnl is none %}<span class="muted">&mdash;</span>
      {% else %}{{ '+' if row.pnl >= 0 else '' }}${{ '%.2f'|format(row.pnl) }}{% endif %}
    </td>
    <td>
      <button type="submit" form="edit-{{ row.symbol }}">Save</button>
      <button type="submit" form="del-{{ row.symbol }}">Remove</button>
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}
  <tr><td colspan="9" class="muted">Nothing tracked yet.</td></tr>
  {% endif %}
</table>
</div>

{#- A form cannot span table cells - browsers close it at the first </td>. The inputs
    above therefore live outside any form and are wired to these by id instead. -#}
{% for row in rows %}
<form id="edit-{{ row.symbol }}" method="post" action="{{ url_for('update') }}" hidden>
  <input type="hidden" name="symbol" value="{{ row.symbol }}">
</form>
<form id="del-{{ row.symbol }}" method="post" action="{{ url_for('remove') }}" hidden>
  <input type="hidden" name="symbol" value="{{ row.symbol }}">
</form>
{% endfor %}

<form class="add" method="post" action="{{ url_for('add') }}">
  <label>Ticker<input class="sym" name="symbol" placeholder="AAPL" autofocus></label>
  <label>Quantity<input name="quantity" placeholder="0"></label>
  <label>Avg cost<input name="cost_basis" placeholder="0.00"></label>
  <button class="primary" type="submit">Add</button>
</form>

<footer>
  Leave quantity at 0 to track a ticker on the watchlist without a position.
  Prometheus metrics are on port {{ metrics_port }} at <code>/metrics</code>.
</footer>
"""


@admin.get("/")
def index():
    with _state_lock:
        holdings = [Holding(h.symbol, h.quantity, h.cost_basis) for h in _holdings]
        quotes = dict(_last_quotes)

    rows = []
    total_value = total_cost = 0.0
    for h in holdings:
        quote = quotes.get(h.symbol)
        price = quote["price"] if quote else None
        value = price * h.quantity if price is not None and h.quantity > 0 else None
        cost = h.cost_basis * h.quantity if h.quantity > 0 else None
        rows.append(
            {
                "symbol": h.symbol,
                "quantity": h.quantity,
                "cost_basis": h.cost_basis,
                "price": price,
                "change_percent": quote["change_percent"] if quote else 0.0,
                "high": quote["high"] if quote else 0.0,
                "low": quote["low"] if quote else 0.0,
                "value": value,
                "pnl": None if value is None or cost is None else value - cost,
            }
        )
        if value is not None and cost is not None:
            total_value += value
            total_cost += cost

    return render_template_string(
        PAGE,
        rows=rows,
        total_value=total_value,
        total_cost=total_cost,
        total_pnl=total_value - total_cost,
        message=request.args.get("msg"),
        interval=POLL_INTERVAL_SECONDS,
        store=TICKERS_FILE,
        metrics_port=METRICS_PORT,
    )


@admin.post("/add")
def add():
    message = add_holding(
        request.form.get("symbol", ""),
        request.form.get("quantity", ""),
        request.form.get("cost_basis", ""),
    )
    return redirect(url_for("index", msg=message))


@admin.post("/update")
def update():
    message = update_holding(
        request.form.get("symbol", ""),
        request.form.get("quantity", ""),
        request.form.get("cost_basis", ""),
    )
    return redirect(url_for("index", msg=message))


@admin.post("/remove")
def remove():
    message = remove_holding(request.form.get("symbol", ""))
    return redirect(url_for("index", msg=message))


def main() -> None:
    load_holdings()
    start_http_server(METRICS_PORT)
    threading.Thread(
        target=lambda: admin.run(host="0.0.0.0", port=ADMIN_PORT, threaded=True),
        daemon=True,
    ).start()
    log.info(
        "metrics on :%d/metrics, portfolio UI on :%d, polling every %ds",
        METRICS_PORT,
        ADMIN_PORT,
        POLL_INTERVAL_SECONDS,
    )
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
