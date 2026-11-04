"""Model-free benchmark over the shipped local catalog; no HTTP or encoder."""
from pathlib import Path
import json
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.catalog import LocalHotelCatalog
from backend.app.geography import find_place
from backend.app.models import TravelIntent


def main() -> int:
    start = time.perf_counter()
    catalog = LocalHotelCatalog(enabled=True)
    loaded_ms = (time.perf_counter() - start) * 1000
    if catalog.status != "ready":
        print(json.dumps(catalog.capabilities(), ensure_ascii=False))
        return 1
    timings = []
    counts = {}
    for city in ("Ларнака", "Пафос"):
        for index in range(250):
            intent = TravelIntent(destination=city, hotel_requirements=["бассейн"] if index % 2 else [], pets=["собака"] if index % 3 == 0 else [])
            started = time.perf_counter()
            result = catalog.search(find_place(city), intent)
            timings.append((time.perf_counter() - started) * 1000)
            counts[city] = len(result.hotels)
    p95 = sorted(timings)[int(len(timings) * .95) - 1]
    print(json.dumps({"catalog": catalog.capabilities(), "python": sys.version.split()[0], "queries": len(timings),
                      "load_ms": round(loaded_ms, 3), "median_ms": round(statistics.median(timings), 3),
                      "p95_ms": round(p95, 3), "max_ms": round(max(timings), 3), "result_counts": counts,
                      "network_calls": 0, "encoder_calls": 0, "llm_calls": 0}, ensure_ascii=False, indent=2))
    return 0 if p95 < 200 and all(counts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
