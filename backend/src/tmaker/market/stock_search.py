from __future__ import annotations

from typing import Any

import httpx

from tmaker.domain.models import WatchSymbol


class EastmoneyStockSearchProvider:
    suggest_url = "https://searchapi.eastmoney.com/api/suggest/get"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=8,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://quote.eastmoney.com/",
            },
        )

    def search(self, query: str, limit: int = 8) -> list[WatchSymbol]:
        response = self.client.get(
            self.suggest_url,
            params={
                "input": query,
                "type": "14",
                "token": "D43BF722C8E33BDC906FB84E98C66E45",
                "count": str(limit),
            },
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("QuotationCodeTable", {}).get("Data") or []
        return _rows_to_watch_symbols(rows, limit)


def _rows_to_watch_symbols(rows: list[dict[str, Any]], limit: int) -> list[WatchSymbol]:
    symbols: list[WatchSymbol] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("UnifiedCode") or row.get("Code") or "").strip()
        name = str(row.get("Name") or "").strip()
        classify = str(row.get("Classify") or "")
        if classify and classify != "AStock":
            continue
        if len(code) != 6 or not code.isdigit() or not name or code in seen:
            continue
        seen.add(code)
        symbols.append(WatchSymbol(symbol=code, name=name))
        if len(symbols) >= limit:
            break
    return symbols
