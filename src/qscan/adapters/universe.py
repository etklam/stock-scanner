"""No-key acquisition of the non-certified English Wikipedia S&P 500 list."""

import json
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from qscan.application.contracts import UniverseObservation

ARTICLE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
LICENSE = "CC BY-SA 4.0; https://creativecommons.org/licenses/by-sa/4.0/"
API_URL = "https://en.wikipedia.org/w/api.php?" + urlencode(
    {
        "action": "parse",
        "page": "List_of_S%26P_500_companies",
        "prop": "text|revid",
        "curtimestamp": "1",
        "format": "json",
        "formatversion": "2",
        "origin": "*",
    }
)


class UniverseSourceError(ValueError):
    pass


class _ConstituentsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_cell = False
        self.cell: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("id") == "constituents":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.row:
            self.rows.append(self.row)
        elif self.in_table and tag == "table":
            self.in_table = False


def _download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "qscan/0.1 universe refresh"})
    with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS URL
        return bytes(response.read())


class WikipediaSP500Source:
    def __init__(self, fetch: Callable[[str], bytes] = _download) -> None:
        self._fetch = fetch

    def fetch(self) -> UniverseObservation:
        try:
            payload = json.loads(self._fetch(API_URL))
            parsed = payload["parse"]
            parser = _ConstituentsParser()
            parser.feed(parsed["text"])
            if not parser.rows or "Symbol" not in parser.rows[0]:
                raise UniverseSourceError("Wikipedia constituents table is missing")
            symbol_index = parser.rows[0].index("Symbol")
            symbols = tuple(row[symbol_index].upper() for row in parser.rows[1:] if row)
            if any(symbol_index >= len(row) or not row[symbol_index] for row in parser.rows[1:]):
                raise UniverseSourceError("Wikipedia constituents row is malformed")
            return UniverseObservation(
                source_url=ARTICLE_URL,
                source_license=LICENSE,
                source_revision=str(parsed["revid"]),
                observed_at=datetime.fromisoformat(payload["curtimestamp"].replace("Z", "+00:00")),
                effective_date=None,
                symbols=symbols,
            )
        except UniverseSourceError:
            raise
        except Exception as exc:
            raise UniverseSourceError("Unable to acquire a valid Wikipedia universe") from exc
