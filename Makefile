.PHONY: help setup install test-unit build clean

help: ## Show this help message
	@echo "Celuma API - Available Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Setup virtual environment
	python3 -m venv .venv
	@echo "Virtual environment created. Activate it with: source .venv/bin/activate"

install: ## Install dependencies
	python3 -m pip install -r requirements.txt

test-unit: ## Run unit tests with coverage
	python3 -m pytest --cov=app --cov-branch --cov-report=xml --cov-report=term-missing --cov-report=html

build: ## Build Docker image
	docker build -t celuma-backend .

clean: ## Clean up Docker images and containers
	docker system prune -f
	docker image rm celuma-backend 2>/dev/null || true
