"""ans-bench as a service. One FastAPI app, one process, run with uvicorn:

  GET  /                                the page (bench/server/static/index.html)
  POST /api/benchmark                   {"agent": "<host|ans name>", "suite": "generated", "force": false}
  GET  /api/benchmark/{host}            same, for shareable links (?suite=&force=)
  GET  /api/stream/{host}               SSE: one `step` event per pipeline step, then `done`
  GET  /api/examples                    cached reports as [{host, score, status, ...}] — never runs anything
  POST /a2a                             A2A JSON-RPC (message/send) — we are an ANS participant
  GET  /.well-known/agent-card.json     our own card
  GET  /.well-known/ans/trust-card.json our own trust card
  GET  /health

The sync pipeline runs in a thread pool; completed reports are cached in memory by
(host, suite) and served with "cached": true (see runs.py — this is demo insurance).
Env: PORT (8000), ANTHROPIC_API_KEY (generated suite), BENCH_PUBLIC_URL, BENCH_LIVE=0
for mock mode, BENCH_WORKERS, BENCH_CONFIG.
"""
from __future__ import annotations
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..llm import have_key
from ..oracles.registry import SPECS
from . import a2a, cards
from .runs import RunStore, SUITES, cached_steps, parse_agent, report_payload, resolve_name

STATIC = Path(__file__).resolve().parent / "static"
LIVE = os.getenv("BENCH_LIVE", "1") not in ("0", "false", "no")
store = RunStore(config_path=os.getenv("BENCH_CONFIG", "config.yaml"), live=LIVE,
                 workers=int(os.getenv("BENCH_WORKERS", "3")))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    n = await asyncio.to_thread(store.warm)
    print(f"[bench.server] {'live' if LIVE else 'MOCK'} mode; {n} report(s) warmed from {store.out_dir}/; "
          f"generator key {'present' if have_key() else 'MISSING'}; public url {cards.PUBLIC_URL}")
    if LIVE and os.getenv("BENCH_PREWARM", "x") != "":
        import threading
        threading.Thread(target=store.prewarm, name="prewarm", daemon=True).start()   # never blocks startup
    yield


app = FastAPI(title=cards.NAME, version=cards.VERSION, lifespan=_lifespan, docs_url=None, redoc_url=None)


class BenchmarkRequest(BaseModel):
    agent: str
    suite: str = "generated"
    force: bool = False


def _host_or_400(agent: str) -> str:
    host = parse_agent(agent)
    if not host and "." not in (agent or ""):
        host = resolve_name(agent, store)          # a bare name: the registry's first-label match
        if not host:
            raise HTTPException(400, f"no registered agent is called {agent.strip()!r}; we tried it as a host, "
                                     f"a first label and a display name — pick one from the table")
    if not host:
        raise HTTPException(400, f"could not read an agent host or ans:// name from {agent!r}")
    return host


def _suite_or_400(suite: str) -> str:
    if suite not in SUITES:
        raise HTTPException(400, f"suite must be one of {SUITES}")
    return suite


async def _benchmark(host: str, suite: str, force: bool) -> dict:
    try:
        report, cached = await asyncio.to_thread(store.wait_report, host, suite, force)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return report_payload(report, cached)


# ---- page ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    p = STATIC / "index.html"
    return p.read_text() if p.exists() else "<!doctype html><title>ans-bench</title><pre>page not built yet — see /api/benchmark</pre>"


# ---- API ----------------------------------------------------------------------------
@app.post("/api/benchmark")
async def benchmark_post(req: BenchmarkRequest) -> dict:
    return await _benchmark(_host_or_400(req.agent), _suite_or_400(req.suite), req.force)


@app.get("/api/benchmark/{host}")
async def benchmark_get(host: str, suite: str = "generated", force: bool = False) -> dict:
    return await _benchmark(_host_or_400(host), _suite_or_400(suite), force)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream(host: str, suite: str, force: bool) -> AsyncIterator[str]:
    job, report = store.get_or_start(host, suite, force)
    if report is not None:                          # cache hit: replay the ticks instantly
        for s in cached_steps(report):
            yield _sse("step", s)
        yield _sse("done", report_payload(report, cached=True))
        return
    assert job is not None
    idx = 0
    while True:
        events = await asyncio.to_thread(job.wait, idx, 15.0)
        for ev in events:
            yield _sse(ev["event"], ev["data"])
        idx += len(events)
        if job.done and idx >= len(job.events):
            break
        if not events:
            yield ": keepalive\n\n"
    if job.error:
        yield _sse("error", {"message": job.error})
    elif job.report is not None:
        yield _sse("done", report_payload(job.report, cached=False))


@app.get("/api/examples")
async def examples(limit: int = 8) -> list[dict]:
    return store.examples(max(1, min(limit, 50)))


@app.get("/api/stream/{host}")
async def stream(host: str, suite: str = "generated", force: bool = False) -> StreamingResponse:
    host, suite = _host_or_400(host), _suite_or_400(suite)
    return StreamingResponse(_stream(host, suite, force), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- A2A: we are a participant -----------------------------------------------------
@app.post("/a2a")
async def a2a_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(a2a._err(None, a2a._PARSE_ERROR, "invalid JSON"), status_code=200)
    resp = await asyncio.to_thread(a2a.handle, body, store, cards.PUBLIC_URL)
    return JSONResponse(resp)


@app.get("/.well-known/agent-card.json")
async def agent_card() -> dict:
    return cards.agent_card()


@app.get("/.well-known/agent.json")            # older A2A discovery path, same card
async def agent_json() -> dict:
    return cards.agent_card()


@app.get("/.well-known/ans/trust-card.json")
async def trust_card() -> dict:
    return cards.trust_card()


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "name": cards.NAME, "version": cards.VERSION, "ans_name": cards.ANS_NAME,
            "live": LIVE, "generator_key": have_key(), "oracles": len(SPECS), **store.stats()}
