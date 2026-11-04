"""Bounded, credential-free research probe of public hotel rate pages.

This is a research utility, not a runtime provider. City-guide observations
have no requested-date/availability guarantee and cannot establish trip totals.
Raw pages and the report are saved only under ignored tmp/ by default.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys

import httpx


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def text_from_html(raw: str) -> str:
    parser = VisibleText()
    parser.feed(raw)
    return " ".join(" ".join(parser.parts).split())


TARGETS = {
    "athens_apollo": "https://www.apollonhotel-athens.com/rates.php",
    "athens_park": "https://www.theparkhotel.gr/en/",
    "istanbul_saruhan": "https://www.saruhanhotel.com/en/rates.html",
}


def inspect(name: str, url: str, status: int, final_url: str, raw: str) -> dict:
    visible = text_from_html(raw)
    result: dict = {
        "source_id": name, "url": url, "final_url": final_url,
        "status": status, "html_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_dates_matched": False, "availability_verified": False,
        "quote": None,
    }
    if name == "athens_apollo":
        match = re.search(r"Prices EURO / night.*?Prices include breakfast", visible, re.I)
        if match:
            result["published_tariff_evidence"] = match[0][:900]
            result["tariff_year"] = None
    elif name == "istanbul_saruhan":
        matches = list(re.finditer(r"STANDARD DOUBLE/TWIN ROOM\s*:\s*(\d+)-(\d+) EURO", visible))
        result["published_ranges"] = [
            {"min_amount": match[1], "max_amount": match[2], "currency": "EUR",
             "unit": "room_per_night", "evidence": match[0]}
            for match in matches[:2]
        ]
        result["tariff_year"] = None
    return result


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--ids", default=",".join(TARGETS))
    args.add_argument("--output", default="tmp/hotel-probes/repeat")
    options = args.parse_args()
    selected = options.ids.split(",")
    if any(name not in TARGETS for name in selected):
        args.error("Only the fixed public research target IDs are accepted")
    output = Path(options.output)
    output.mkdir(parents=True, exist_ok=True)
    report = []
    # No auth, no cookies, no proxy, no challenge handling, no retries.
    with httpx.Client(timeout=20, follow_redirects=True, max_redirects=4) as client:
        for name in selected:
            url = TARGETS[name]
            try:
                response = client.get(url)
                raw = response.text
                (output / f"{name}.html").write_text(raw, encoding="utf-8")
                report.append(inspect(name, url, response.status_code, str(response.url), raw))
            except httpx.HTTPError as exc:
                report.append({"source_id": name, "url": url, "error": type(exc).__name__, "quote": None})
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
