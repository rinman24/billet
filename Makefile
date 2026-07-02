.PHONY: help install lint format imports test test-quick build docs-assets docs-build docs-serve

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Sync the locked dev environment (.venv)
	uv sync --frozen

lint: ## Ruff check + Pyright (strict); must report 0 errors
	uv run ruff check .
	uv run pyright

format: ## Apply Ruff formatting
	uv run ruff format .

imports: ## Enforce the closed-architecture layer import graph
	uv run lint-imports

test: ## Run the test suite with coverage
	uv run pytest --cov=billet --cov-report=term-missing

test-quick: ## Run the test suite without coverage
	uv run pytest --no-cov

build: ## Build sdist + wheel
	uv build

docs-assets: ## Copy the brand kit into docs/ (generated; gitignored)
	rm -rf docs/brand
	cp -R brand docs/brand

docs-build: docs-assets ## Build the MkDocs site (strict)
	uv run mkdocs build --strict

docs-serve: docs-assets ## Serve the docs locally
	uv run mkdocs serve
