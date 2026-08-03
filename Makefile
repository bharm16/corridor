.PHONY: boot up down psql test

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
