.PHONY: install selftest run run-live probe test serve clean

install:
	pip install -r requirements.txt

selftest:          ## Verify the oracles are honest in THIS environment. Run first.
	python -m bench selftest

run:               ## Offline: mock agent, live oracles, 26 assertions
	python -m bench run --no-gen

run-gen:           ## Offline + LangGraph generator (needs ANTHROPIC_API_KEY)
	python -m bench run

run-live:          ## The real thing: live ANS + live agent
	python -m bench run --live

probe:             ## Print real registry + agent shapes (do this before run-live)
	python -m bench probe-registry --live
	python -m bench probe-agent --live

test:
	python -m pytest -q tests

serve:             ## The benchmark as a service + A2A participant, on $PORT (default 8000)
	uvicorn bench.server.app:app --host 0.0.0.0 --port $${PORT:-8000}

clean:
	rm -rf out/*.json .pytest_cache **/__pycache__
