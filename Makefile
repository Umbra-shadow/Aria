# Aria — make dev / demo / test

PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: dev demo test clean

dev:  ## run the FastAPI app with reload (Qwen brain via .env or X-Qwen-Key)
	$(PY) -m uvicorn aria.app:get_app --factory --reload --port 8001

demo:  ## serve Aria's split-screen UI + API at :8001
	@test -f .env || (echo "→ creating .env from .env.example"; cp .env.example .env)
	@echo "→ Aria at http://localhost:8001  (enter your Qwen key in the UI)"
	$(PY) -m uvicorn aria.app:get_app --factory --host 0.0.0.0 --port 8001

test:
	$(PY) -m pytest

clean:
	rm -rf .pytest_cache engine/__pycache__ aria/__pycache__ engine/tests/__pycache__
