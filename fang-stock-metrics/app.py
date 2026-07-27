import logging
import os
import time

import requests
from prometheus_client import Gauge, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fang-stock-metrics")

TICKERS = ["META", "AMZN", "NFLX", "GOOGL"]
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8000"))
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"

stock_price = Gauge(
    "fang_stock_price_usd",
    "Latest price in USD for a FANG stock ticker",
    ["ticker"],
)
scrape_errors = Gauge(
    "fang_stock_scrape_errors_total",
    "Number of consecutive failed price fetches for a ticker",
    ["ticker"],
)


def poll_once() -> None:
    for ticker in TICKERS:
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
            log.info("ticker=%s price=%.2f", ticker, price)
        except Exception:
            scrape_errors.labels(ticker=ticker).inc()
            log.exception("failed to fetch price for ticker=%s", ticker)


def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("serving metrics on :%d/metrics, polling every %ds", METRICS_PORT, POLL_INTERVAL_SECONDS)
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
