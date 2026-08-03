.PHONY: boot up down psql test corpus demo ingest docs queue agreements milestones exceptions minutes

# One command from a clean clone.
boot:
	uv sync
	docker compose up -d --wait
	uv run alembic upgrade head

up:
	docker compose up -d --wait

down:
	docker compose down

psql:
	docker compose exec postgres psql -U corridor -d corridor

test:
	uv run pytest

# Resolve corpus/manifest.yaml to files on disk. Re-running is a no-op for
# unchanged sources; a source whose bytes changed keeps both revisions.
corpus:
	uv run python -m corridor.corpus

# Raw files -> cited report. Pass N to accept only the first N candidates:
#   make demo LIMIT=25
demo:
	uv run python -m corridor.demo $(LIMIT)

# Load every fetched source in the manifest lockfile.
ingest:
	uv run python -m corridor.docs ingest

# Browse the evidence store: `make docs` or `make docs ARGS="page 167 1"`.
docs:
	uv run python -m corridor.docs $(ARGS)

# Adjudication queue at http://localhost:8412
queue:
	uv run uvicorn corridor.web.app:app --port 8412 --reload

# LLM extraction over the executed agreements. Needs OPENAI_API_KEY in .env.
agreements:
	uv run python -m corridor.extract_agreement $(ARGS)

# Import a milestone CSV: make milestones ARGS="sh99-grand-parkway corpus/sh99-milestones.csv RELO-CONSTR"
milestones:
	uv run python -m corridor.milestones $(ARGS)

# Current exception list: make exceptions ARGS="nhhip-3c2"
exceptions:
	uv run python -m corridor.exceptions $(ARGS)

# LLM extraction over coordination meeting notes. Needs OPENAI_API_KEY.
minutes:
	uv run python -m corridor.extract_minutes $(ARGS)
