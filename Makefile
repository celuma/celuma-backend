.PHONY: help setup install test build run clean

help: ## Show this help message
	@echo "Celuma API - Available Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Setup virtual environment and load environment variables
	python -m venv .venv
	@echo "Virtual environment created. Activate it with: source .venv/bin/activate"

install: ## Install dependencies
	source .venv/bin/activate && pip install -r requirements.txt

test: ## Run all tests
	source .venv/bin/activate && cd tests && python run_all_tests.py

build: ## Build Docker container
	docker build -t celuma-backend .

run: ## Run the API server
	docker-compose up -d

clean: ## Clean up containers and images
	docker-compose down -v
	docker system prune -f
