# Makefile for Nexus Search Hardening Review
# Canonical commands for coverage, testing, and validation

.PHONY: test test-cov test-full lint typecheck security mutate

# Canonical coverage command (used in CI)
# Runs full suite with line + branch coverage on all nexus_search modules
test-cov:
	.venv\Scripts\python.exe -m pytest \
		--cov=nexus_search \
		--cov-report=term-missing \
		--cov-report=xml:docs/evidence/coverage.xml \
		--cov-branch \
		-q -p no:randomly \
		2>&1 | tee docs/evidence/coverage_$(shell date +%Y%m%d_%H%M%S).log

# Full test suite (no coverage)
test:
	.venv\Scripts\python.exe -m pytest -q -p no:randomly

# Full test suite with coverage (alias)
test-full: test-cov

# Lint
lint:
	.venv\Scripts\python.exe -m ruff check nexus_search tests
	.venv\Scripts\python.exe -m ruff format --check nexus_search tests

# Type check
typecheck:
	.venv\Scripts\python.exe -m mypy nexus_search --strict --ignore-missing-imports

# Security scan
security:
	.venv\Scripts\python.exe -m bandit -r nexus_search -f json -o docs/evidence/bandit_$(shell date +%Y%m%d_%H%M%S).json
	.venv\Scripts\python.exe -m pip_audit --format=json --output=docs/evidence/pip_audit_$(shell date +%Y%m%d_%H%M%S).json

# Mutation testing (requires Linux/WSL)
mutate:
	@echo "Run in Linux CI: mutmut run --paths-to-mutate nexus_search/rag/sanitize.py nexus_search/crawler/security.py nexus_search/core/storage.py nexus_search/ingestion/connectors/files.py nexus_search/rag/session.py"

# Fresh clone test
fresh-clone:
	powershell -ExecutionPolicy Bypass -File scripts/dev/fresh_clone_test.ps1

# Load smoke
load-smoke:
	$env:NEXUS_RUN_LOAD_SMOKE=1; .venv\Scripts\python.exe -m pytest tests/test_load_smoke.py -q

# Soak test
soak:
	$env:NEXUS_RUN_SOAK=1; .venv\Scripts\python.exe -m pytest tests/test_soak.py -q

# Schemathesis fuzz
schemathesis:
	.venv\Scripts\python.exe -m schemathesis run --hypothesis-max-examples=900 --checks=all http://localhost:8000/openapi.json --base-url=http://localhost:8000

# Real embedder test (CPU)
real-embedder:
	$env:NEXUS_RUN_MODEL_TESTS=1; .venv\Scripts\python.exe -m pytest tests/evaluation/ -q

# Coverage for specific high-priority modules
cov-egress:
	.venv\Scripts\python.exe -m pytest tests/rag/ -q --cov=nexus_search.rag.egress --cov-report=term-missing --cov-branch -p no:randomly

cov-files:
	.venv\Scripts\python.exe -m pytest tests/ingestion/ -q --cov=nexus_search.ingestion.connectors.files --cov-report=term-missing --cov-branch -p no:randomly

cov-fetcher:
	.venv\Scripts\python.exe -m pytest tests/crawler/test_fetcher.py -q --cov=nexus_search.crawler.fetcher --cov-report=term-missing --cov-branch -p no:randomly

cov-api:
	.venv\Scripts\python.exe -m pytest tests/core/test_api.py -q --cov=nexus_search.core.api --cov-report=term-missing --cov-branch -p no:randomly

cov-gate:
	.venv\Scripts\python.exe -m pytest tests/rag/test_gate.py -q --cov=nexus_search.rag.gate --cov-report=term-missing --cov-branch -p no:randomly

cov-session:
	.venv\Scripts\python.exe -m pytest tests/rag/test_sessions.py tests/rag/test_session_coverage.py -q --cov=nexus_search.rag.session --cov-report=term-missing --cov-branch -p no:randomly

cov-sanitize:
	.venv\Scripts\python.exe -m pytest tests/rag/test_sanitize.py tests/rag/test_sanitize_fuzz.py tests/rag/test_sanitize_scrubber.py -q --cov=nexus_search.rag.sanitize --cov-report=term-missing --cov-branch -p no:randomly

cov-storage:
	.venv\Scripts\python.exe -m pytest tests/core/test_storage.py tests/core/test_storage_coverage.py -q --cov=nexus_search.core.storage --cov-report=term-missing --cov-branch -p no:randomly

# Mutation check specific tests
mutate-egress:
	@echo "Manual: break egress.py moderation logic, confirm tests fail"

mutate-files:
	@echo "Manual: break files.py encoding/zip logic, confirm tests fail"

mutate-sanitize:
	@echo "Manual: break sanitize.py leak scrubber, confirm tests fail"

mutate-session:
	@echo "Manual: break session.py TTL/prune logic, confirm tests fail"

mutate-storage:
	@echo "Manual: break storage.py atomic writes, confirm tests fail"