"""The benchmark as a service, and as an ANS participant.

  app.py    FastAPI: the page, /api/benchmark, /api/stream (SSE), /a2a, our own cards
  runs.py   run store: in-memory cache keyed by (host, suite), in-flight joins, event log
  cards.py  our own agent card and trust card — written so our own pipeline can read them
  a2a.py    the A2A JSON-RPC handler and the plain-text summary it answers with
"""
