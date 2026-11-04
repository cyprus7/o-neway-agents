"""Frozen 20-trip public-provider measurement; never calls a language model.

Without --live, validate and print the frozen manifest only. --docker runs the
same harness inside the already rebuilt app image, reaching its internal worker.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
FROZEN_MANIFEST_SHA256 = "2fad50a486384dd0065ccbce5789f993394deffdb22dba4edb62f627c9cb05f5"
FROZEN_REPOSITORY_SHA256 = "475b4389e7f0f11c1f1d5787411e20ca606b698bf08821ff4909b90d813da018"
DENOMINATOR = 20
MINIMUM_BOTH = 14
DEFAULT_MANIFEST = ROOT / "specs" / "002-travel-pricing" / "benchmark.json"
sys.path.insert(0, str(ROOT))


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def anonymous_request(request: Any, worker_url: Any) -> bool:
    """Drop an HTTP client's automatic tracking cookie before actual dispatch.

    Never acquire/replay a logged-in browser session. Authentication and model
    routes remain prohibited; a server Set-Cookie is not permission to use one.
    """
    allowed_post = request.url.host == worker_url.host and request.url.port == worker_url.port and request.url.path == "/v1/quotes"
    if request.method != "GET" and not (request.method == "POST" and allowed_post):
        raise RuntimeError("Non-provider POST blocked; no LLM requests are permitted")
    if request.url.host in ("inference.hetzner.com", "api.openai.com") or any(part in request.url.path for part in ("/chat/completions", "/embeddings", "/responses")):
        raise RuntimeError("Model endpoint blocked")
    if request.url.userinfo or any(key.casefold() in ("api_key", "apikey", "token", "access_token", "authorization", "session", "sid") for key in request.url.params):
        raise RuntimeError("Credential-like URL is not permitted")
    if any(key in request.headers for key in ("authorization", "x-api-key")):
        raise RuntimeError("Credential-bearing request blocked")
    stripped = "cookie" in request.headers
    if stripped:
        del request.headers["cookie"]
    return stripped


def frozen_manifest(raw: bytes) -> dict:
    # The existing Windows checkout is CRLF while the committed blob is LF.
    # Accept exactly those two pre-frozen byte sequences, not edited JSON.
    if digest(raw) not in (FROZEN_MANIFEST_SHA256, FROZEN_REPOSITORY_SHA256):
        raise ValueError("Frozen benchmark SHA256 differs; do not rewrite or silently reselect cases")
    value = json.loads(raw)
    cases = value.get("cases", [])
    if len(cases) != DENOMINATOR or value.get("denominator") != DENOMINATOR:
        raise ValueError("The immutable denominator must contain all 20 cases")
    if [case["id"] for case in cases] != [f"B{index:03}" for index in range(1, 21)]:
        raise ValueError("Expected original B001–B020 order")
    return value


def summary(rows: list[dict]) -> dict:
    complete = len(rows) == DENOMINATOR
    normalized = sum(row.get("both_quoted_normalized_current", False) for row in rows)
    return {
        "denominator": DENOMINATOR, "completed_cases": len(rows),
        "runner_errors": sum(row.get("runner_error") is not None for row in rows),
        "both_quoted_normalized_current": normalized,
        "both_displayable_reference_or_quote": sum(row.get("both_displayable_reference_or_quote", False) for row in rows),
        "any_displayable_numeric": sum(row.get("any_displayable_numeric", False) for row in rows),
        "flight_quoted_normalized_current": sum(row.get("components", {}).get("flight", {}).get("quoted_normalized_current", False) for row in rows),
        "stay_quoted_normalized_current": sum(row.get("components", {}).get("stay", {}).get("quoted_normalized_current", False) for row in rows),
        "flight_reference": sum(row.get("components", {}).get("flight", {}).get("reference_numeric", False) for row in rows),
        "stay_reference": sum(row.get("components", {}).get("stay", {}).get("reference_numeric", False) for row in rows),
        "minimum_both_normalized": MINIMUM_BOTH,
        "coverage_gate_passed": complete and normalized >= MINIMUM_BOTH,
        "llm_calls": 0,
    }


@dataclass(frozen=True)
class AirportContext:
    """Manifest labels only; no invented geocoordinates or alternate airports."""
    name: str
    airport: str


async def execute(payload: dict, emit: Callable[[dict], None]) -> None:
    # Imports intentionally exclude planner, app startup and every LLM module.
    import httpx
    from backend.app.models import TravelIntent
    from backend.app.storage import Store
    from backend.app.pricing.models import RequestContext
    from backend.app.pricing.normalization import compatible_scope, request_context_hash
    from backend.app.pricing.providers import KeylessPricing
    from backend.app.pricing.aggregation import summarize_prices

    raw = base64.b64decode(payload["manifest_base64"], validate=True)
    manifest = frozen_manifest(raw)
    worker = payload["worker_url"]
    parsed_worker = httpx.URL(worker)
    if parsed_worker.scheme != "http" or parsed_worker.host not in ("price-worker", "127.0.0.1", "localhost", "::1") or parsed_worker.path not in ("", "/") or parsed_worker.userinfo:
        raise ValueError("Only the internal local worker is permitted")
    os.environ["PRICE_WORKER_URL"] = worker
    os.environ["KEYLESS_PRICING_ENABLED"] = "1"
    requests: list[dict] = []
    blocked_llm_attempts = 0

    class ReadOnlyTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            nonlocal blocked_llm_attempts
            try:
                stripped = anonymous_request(request, parsed_worker)
            except RuntimeError as error:
                if str(error) in ("Non-provider POST blocked; no LLM requests are permitted", "Model endpoint blocked"):
                    blocked_llm_attempts += 1
                raise
            record = {"method": request.method, "url": str(request.url), "dispatched_at": utc(),
                      "automatic_cookie_stripped": stripped}
            requests.append(record)
            started = time.monotonic()
            try:
                response = await self.transport.handle_async_request(request)
                record["http_status"] = response.status_code
                return response
            except Exception as error:
                record["error"] = type(error).__name__
                raise
            finally:
                record["headers_latency_seconds"] = round(time.monotonic() - started, 3)

        async def aclose(self):
            await self.transport.aclose()

    code_hashes = {}
    for name in ("providers", "airlines", "lodging", "models", "normalization", "aggregation"):
        path = ROOT / "backend" / "app" / "pricing" / (name + ".py")
        code_hashes[name] = digest(path.read_bytes())
    policies = {"code_sha256": code_hashes, "worker_url": worker,
                "runner_source_sha256": payload["runner_source_sha256"],
                "case_deadline_seconds": 55, "concurrent_cases": payload["concurrency"],
                "case_start_interval_seconds": payload["pace_seconds"],
                "api_keys_used": False, "llm_calls_allowed": 0, "cache": "isolated_in_memory_cold_start"}
    run_id = uuid4().hex
    emit({"event": "freeze", "run_id": run_id, "kind": payload["kind"], "started_at": utc(),
          "manifest_sha256": digest(raw), "manifest_cases_sha256": digest(canonical(manifest["cases"])),
          "repository_manifest_sha256": FROZEN_REPOSITORY_SHA256,
          "manifest": manifest, "provider_policy_sha256": digest(canonical(policies)), "policies": policies,
          "layer": "live_keyless_providers_not_model_evaluation", "results": []})
    store = Store(":memory:")
    semaphore = asyncio.Semaphore(payload["concurrency"])
    rows: list[dict] = []
    started_all = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=ReadOnlyTransport(), follow_redirects=False,
                headers={"User-Agent": "OneRequest-LocalPoC/0.1 (bounded public-fare research; no booking)"},
                limits=httpx.Limits(max_connections=8), timeout=12, trust_env=False) as client:
            pricing = KeylessPricing(client, store)

            async def one_case(case: dict, index: int) -> dict:
                # Pacing prevents the harness itself exhausting six airline dispatches/minute.
                await asyncio.sleep(max(0, started_all + index * payload["pace_seconds"] - time.monotonic()))
                async with semaphore:
                    started = time.monotonic()
                    case_snapshot = json.loads(json.dumps(case))
                    row: dict[str, Any] = {"id": case["id"], "context": case_snapshot,
                        "context_sha256": digest(canonical(case_snapshot)), "started_at": utc(), "components": {},
                        "runner_error": None, "both_quoted_normalized_current": False,
                        "both_displayable_reference_or_quote": False, "any_displayable_numeric": False}
                    quotes = {}
                    try:
                        intent = TravelIntent(origin=case["origin_iata"], destination=case["stay_destination"],
                            date_from=case["date_from"], date_to=case["date_to"], adults=case["adults"],
                            children_ages=case["children_ages"], rooms=case["rooms"], currency=case["currency"])
                        origin = AirportContext(case["origin_iata"], case["origin_iata"])
                        place = AirportContext(case["stay_destination"], case["destination_iata"])
                        contexts = {kind: RequestContext(component=kind, origin=origin.airport if kind == "flight" else None,
                            destination=place.airport, date_from=case["date_from"], date_to=case["date_to"],
                            adults=case["adults"], children_ages=tuple(case["children_ages"]), rooms=case["rooms"],
                            currency=case["currency"], trip_type=case["trip_type"] if kind == "flight" else "not_applicable")
                            for kind in ("flight", "stay")}
                        row["request_contexts"] = {kind: context.model_dump(mode="json") for kind, context in contexts.items()}
                        row["request_context_hashes"] = {kind: request_context_hash(context) for kind, context in contexts.items()}
                        async def component(kind):
                            before = time.monotonic()
                            quote, diagnostics, _ = await pricing._find(kind, contexts[kind], place, origin, intent)
                            numeric = quote.value.kind != "unknown"
                            verified = (quote.outcome == "quoted" and quote.normalized_group is not None
                                        and quote.normalized_group.kind != "unknown" and quote.freshness() in ("live", "cached")
                                        and quote.request_context_hash == request_context_hash(contexts[kind])
                                        and compatible_scope(contexts[kind], quote.scope))
                            reference = numeric and quote.outcome == "reference"
                            quotes[kind] = quote
                            row["components"][kind] = {"latency_seconds": round(time.monotonic() - before, 3),
                                "quoted_normalized_current": bool(verified), "reference_numeric": reference,
                                "displayable_numeric": reference or bool(verified),
                                "observation": quote.public_dict(), "diagnostics": diagnostics}

                        async with asyncio.timeout(55):
                            await asyncio.gather(component("flight"), component("stay"))
                        if digest(canonical(case)) != row["context_sha256"]:
                            raise RuntimeError("Immutable manifest case was mutated")
                        row["pricing_summary"] = summarize_prices(quotes, currency=case["currency"]).model_dump(mode="json")
                    except Exception as error:
                        row["runner_error"] = type(error).__name__
                        if isinstance(error, RuntimeError):
                            safe_errors = {"Non-provider POST blocked; no LLM requests are permitted", "Model endpoint blocked",
                                           "Credential-like URL is not permitted", "Credential-bearing request blocked",
                                           "Cannot send a request, as the client has been closed.", "Immutable manifest case was mutated"}
                            row["runner_error_detail"] = str(error) if str(error) in safe_errors else "unspecified_runtime_error"
                    # A failed/timed-out component must not erase a useful sibling
                    # observation, nor leave the failed case out of the denominator.
                    row["both_quoted_normalized_current"] = all(row["components"].get(kind, {}).get("quoted_normalized_current", False) for kind in ("flight", "stay"))
                    row["both_displayable_reference_or_quote"] = all(row["components"].get(kind, {}).get("displayable_numeric", False) for kind in ("flight", "stay"))
                    row["any_displayable_numeric"] = any(item.get("displayable_numeric", False) for item in row["components"].values())
                    row["latency_seconds"] = round(time.monotonic() - started, 3)
                    row["finished_at"] = utc()
                    rows.append(row)
                    emit({"event": "case", "result": row, "summary": summary(rows)})
                    return row

            await asyncio.gather(*(one_case(case, index) for index, case in enumerate(manifest["cases"])))
        emit({"event": "finish", "finished_at": utc(), "elapsed_seconds": round(time.monotonic() - started_all, 3),
              "summary": summary(rows), "metrics": store.metrics(), "http_requests": requests,
              "blocked_model_attempts": blocked_llm_attempts, "llm_calls": 0})
    finally:
        store.close()


def main(kind: str = "feasibility", require_coverage: bool = False) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--live", action="store_true", help="Perform real public-provider requests; no LLM")
    parser.add_argument("--docker", action="store_true", help="Use the already rebuilt local Compose app image")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker-url", default="http://price-worker:8080")
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=2)
    parser.add_argument("--pace-seconds", type=float, default=40, help="At least 40 seconds between case starts")
    args = parser.parse_args()
    if args.pace_seconds < 40:
        parser.error("At least 40 seconds are required to respect the airline dispatch budget")
    raw = args.manifest.read_bytes()
    manifest = frozen_manifest(raw)
    if not args.live:
        print(json.dumps({"status": "frozen_not_executed", "manifest_sha256": digest(raw),
                          "cases": len(manifest["cases"]), "denominator": DENOMINATOR, "llm_calls": 0,
                          "minimum_both_normalized": MINIMUM_BOTH}))
        return
    target = args.report or ROOT / "tmp" / "pricing-probe" / f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    if target.exists():
        raise SystemExit("Report already exists; choose a new path to retain prior observations")
    target.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"status": "starting", "results": []}

    def receive(event):
        if event["event"] == "freeze":
            report.update({key: value for key, value in event.items() if key != "event"})
            report["status"] = "running"
        elif event["event"] == "case":
            report["results"].append(event["result"])
            report["results"].sort(key=lambda row: row["id"])
            report["summary"] = event["summary"]
        elif event["event"] == "finish":
            report.update({key: value for key, value in event.items() if key != "event"})
            report["status"] = "completed"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": event["event"], "case": event.get("result", {}).get("id"),
            "summary": event.get("summary"), "manifest_sha256": report.get("manifest_sha256")}, ensure_ascii=True), flush=True)

    source = Path(__file__).read_bytes()
    payload = {"manifest_base64": base64.b64encode(raw).decode(), "worker_url": args.worker_url,
               "runner_source_sha256": digest(source),
               "concurrency": args.concurrency, "pace_seconds": args.pace_seconds, "kind": kind}
    if args.docker:
        encoded = base64.b64encode(canonical(payload)).decode()
        encoded_source = base64.b64encode(source).decode()
        # The runtime image can predate this harness. Execute our checked local
        # source in memory; do not mutate the image or write production files.
        bootstrap = ("import asyncio,base64,json,sys,types\n"
                     "module=types.ModuleType('pricing_probe_embedded')\n"
                     "module.__file__='/app/scripts/pricing_probe.py'\n"
                     "sys.modules[module.__name__]=module\n"
                     f"exec(compile(base64.b64decode('{encoded_source}'),module.__file__,'exec'),module.__dict__)\n"
                     f"payload=json.loads(base64.b64decode('{encoded}'))\n"
                     "asyncio.run(module.execute(payload,lambda event: print('PRICING_EVENT:'+json.dumps(event,ensure_ascii=True),flush=True)))\n")
        process = subprocess.Popen(["docker", "compose", "exec", "-T", "app", "python", "-"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8")
        assert process.stdin and process.stdout
        process.stdin.write(bootstrap)
        process.stdin.close()
        for line in process.stdout:
            if line.startswith("PRICING_EVENT:"):
                receive(json.loads(line.removeprefix("PRICING_EVENT:")))
        if process.wait() != 0:
            report["status"] = "runner_failed"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise SystemExit("Container probe failed; partial report retained")
    else:
        asyncio.run(execute(payload, receive))
    if report.get("status") != "completed" or len(report["results"]) != DENOMINATOR:
        raise SystemExit("Incomplete 20-case measurement; keep failures in the denominator")
    print(json.dumps({"report": str(target), "summary": report["summary"]}, ensure_ascii=True))
    if require_coverage and not report["summary"]["coverage_gate_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
