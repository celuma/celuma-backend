run:
	docker compose up --build

revision:
	docker compose exec api alembic revision -m "$(m)" --autogenerate

migrate:
	docker compose exec api alembic upgrade head
