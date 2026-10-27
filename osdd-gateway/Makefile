# ==============================================================================
# OSDD Protocol Gateway — Makefile
# ==============================================================================
# Usage:
#   make install      — Install production dependencies
#   make dev          — Install dev/test dependencies
#   make lint         — Run linters (ruff, mypy)
#   make test         — Run test suite via pytest
#   make run-modbus   — Start gateway with Modbus driver
#   make run-mqtt     — Start gateway with MQTT driver
#   make run-opcua    — Start gateway with OPC-UA driver
#   make clean        — Remove build artefacts and caches
# ==============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
CONFIG ?= config/example_modbus.yaml

# ── Paths ────────────────────────────────────────────────────────────────────
SRC_DIR    := src
TEST_DIR   := tests
CACHE_DIRS := .mypy_cache .pytest_cache .ruff_cache __pycache__ build dist *.egg-info

# ── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Install ──────────────────────────────────────────────────────────────────
.PHONY: install
install: ## Install production dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

.PHONY: dev
dev: ## Install development & testing dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt
	$(PIP) install -e .

# ── Lint ─────────────────────────────────────────────────────────────────────
.PHONY: lint
lint: ## Run ruff linter + mypy type checker
	$(PYTHON) -m ruff check $(SRC_DIR) $(TEST_DIR)
	$(PYTHON) -m mypy $(SRC_DIR) --ignore-missing-imports

# ── Test ─────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run the test suite with pytest
	$(PYTHON) -m pytest $(TEST_DIR) -v --tb=short

# ── Run ──────────────────────────────────────────────────────────────────────
.PHONY: run-modbus
run-modbus: ## Start the gateway with the Modbus driver
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m gateway.main \
		--config $(CONFIG) \
		--protocol modbus \
		--log-level INFO

.PHONY: run-mqtt
run-mqtt: ## Start the gateway with the MQTT driver
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m gateway.main \
		--config config/example_mqtt.yaml \
		--protocol mqtt \
		--log-level INFO

.PHONY: run-opcua
run-opcua: ## Start the gateway with the OPC-UA driver
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m gateway.main \
		--config config/example_opcua.yaml \
		--protocol opcua \
		--log-level INFO

# ── Clean ────────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove build artefacts and caches
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf $(CACHE_DIRS) htmlcov .coverage
	@echo "Cleaned."
