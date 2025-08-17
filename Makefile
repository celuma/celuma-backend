.PHONY: help test test-all test-flow test-validation test-performance test-cleanup install run clean

help: ## Show this help message
	@echo "Celuma API - Available Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	pip install -r requirements.txt

run: ## Run the API server
	docker-compose up -d

stop: ## Stop the API server
	docker-compose down

logs: ## Show API logs
	docker logs celuma-backend-api-1 -f

test: ## Run all tests
	cd tests && python run_all_tests.py

test-all: test ## Alias for running all tests

test-flow: ## Run complete flow tests only
	cd tests && python test_endpoints.py

test-validation: ## Run validation tests only
	cd tests && python test_validation_errors.py

test-performance: ## Run performance tests only
	cd tests && python test_performance.py

test-cleanup: ## Run test data cleanup analysis
	cd tests && python cleanup_test_data.py

test-interactive: ## Run interactive test menu
	python run_tests.py

clean: ## Clean up test data and containers
	docker-compose down -v
	docker system prune -f

status: ## Check API and database status
	@echo "Checking API status..."
	@curl -s http://localhost:8000/api/v1/health | jq . || echo "API not responding"
	@echo "Checking Docker containers..."
	@docker ps --filter "name=celuma-backend"
