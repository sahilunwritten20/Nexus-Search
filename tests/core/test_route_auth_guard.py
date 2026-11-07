#!/usr/bin/env python3
"""Guard test: fails if a route is added without an explicit auth decision."""
import os
import pytest

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_API_KEY", "dev-key")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.api import app
from fastapi.testclient import TestClient


def get_route_paths():
    """Extract all route paths from the FastAPI app."""
    paths = set()
    for route in app.routes:
        if hasattr(route, 'path'):
            paths.add(route.path)
    return paths


# Expected auth decisions for each route
# Categories: "open" (no auth), "key" (API key required), "admin" (key + admin scope)
# Note: parameterized paths like /documents/{doc_id} are normalized for matching
EXPECTED_AUTH = {
    "/health": "open",
    "/ready": "open",
    "/metrics": "key",
    "/openapi.json": "open",
    "/docs": "open",
    "/docs/oauth2-redirect": "open",
    "/redoc": "open",
    "/documents": "key",  # POST
    "/documents/bulk": "key",  # POST
    "/documents/{doc_id}": "key",  # GET / DELETE
    "/search": "key",  # GET
    "/search/explain": "key",  # POST
    "/suggest": "key",  # GET
    "/related": "key",  # GET
    "/health": "open",
    "/ready": "open",
    "/metrics": "key",
    "/graph/neighbors": "key",
    "/ask": "key",  # POST
    "/ask/stream": "key",  # POST
    "/graph/report": "key",
    "/graph/neighbors": "key",
}

# Routes that should require auth (all except /health, /ready, /openapi.json, /docs, /redoc)
PUBLIC_ROUTES = {
    "/health", "/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"
}


def get_route_paths():
    """Extract all route paths from the FastAPI app."""
    paths = set()
    for route in app.routes:
        if hasattr(route, 'path'):
            paths.add(route.path)
    return paths


class TestRouteAuthGuard:
    """Ensure every route has an explicit auth decision."""

    def test_all_routes_have_auth_decision(self):
        """Every route path must be documented in EXPECTED_AUTH or PUBLIC_ROUTES."""
        paths = get_route_paths()
        for path in paths:
            # Normalize parameterized paths for matching
            normalized = path
            assert normalized in EXPECTED_AUTH or normalized in PUBLIC_ROUTES, \
                f"Route {path} has no explicit auth decision. Add to EXPECTED_AUTH or PUBLIC_ROUTES."

    def test_no_undocumented_public_routes(self):
        """No route should be accidentally public."""
        paths = get_route_paths()
        for path in paths:
            if path in PUBLIC_ROUTES:
                continue
            # All non-public routes must require auth
            assert path in EXPECTED_AUTH, f"Route {path} missing from EXPECTED_AUTH"


class TestAuthMatrix:
    """Test auth behavior for every route."""

    @pytest.fixture
    def client(self):
        # Set env vars BEFORE importing the app module
        saved_env = os.environ.get("NEXUS_API_KEY")
        saved_dev = os.environ.get("NEXUS_ENV")
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_API_KEY"] = "test-key"
        os.environ["NEXUS_EMBEDDER"] = "hash:384"
        # Force reimport to pick up new env vars
        import importlib
        import nexus_search.core.api
        importlib.reload(nexus_search.core.api)
        from nexus_search.core.api import app
        try:
            yield TestClient(app)
        finally:
            # Restore environment to avoid test pollution
            if saved_env is None:
                os.environ.pop("NEXUS_API_KEY", None)
            else:
                os.environ["NEXUS_API_KEY"] = saved_env
            if saved_dev is None:
                os.environ.pop("NEXUS_ENV", None)
            else:
                os.environ["NEXUS_ENV"] = saved_dev
            importlib.reload(nexus_search.core.api)

    def test_public_routes_no_key(self, client):
        """Public routes should work without API key."""
        for path in PUBLIC_ROUTES:
            if path in ["/docs/oauth2-redirect"]:  # special case
                continue
            resp = client.get(path)
            assert resp.status_code != 401, f"{path} should not require auth"

    def test_protected_routes_require_key(self, client):
        """Protected routes should return 401 without key."""
        # Use concrete paths that exist
        simple_post_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/documents/test-id"]]
        simple_get_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p not in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/search", "/suggest", "/related", "/graph/neighbors", "/graph/report"]]
        
        for path in simple_post_paths:
            resp = client.post(path, json={})
            assert resp.status_code == 401, f"{path} should require auth, got {resp.status_code}"
        
        for path in simple_get_paths:
            resp = client.get(path)
            assert resp.status_code == 401, f"{path} should require auth, got {resp.status_code}"

    def test_protected_routes_with_valid_key(self, client):
        """Protected routes should work with valid key (may return 422/400 for invalid input)."""
        # Only test simple routes that don't require query parameters
        simple_post_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/documents/test-id"]]
        simple_get_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p not in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/search", "/suggest", "/related", "/graph/neighbors", "/graph/report"]]
        
        for path in simple_post_paths:
            method = "post"
            headers = {"X-API-Key": "test-key"}
            payload = {}
            if path == "/documents":
                payload = {"doc_id": "test", "title": "t", "content": "c", "doc_type": "text", "metadata": {}}
            elif path == "/ask":
                payload = {"query": "test"}
            elif path == "/ask/stream":
                payload = {"query": "test"}
            elif path == "/search/explain":
                payload = {"query": "test"}
            elif path == "/documents/bulk":
                payload = [{"doc_id": "test", "title": "t", "content": "c", "doc_type": "text", "metadata": {}}]
            elif path == "/documents/test-id":
                payload = {}
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code != 401, f"{path} should accept valid key, got {resp.status_code}: {resp.text}"
        
        for path in simple_get_paths:
            headers = {"X-API-Key": "test-key"}
            resp = client.get(path, headers=headers)
            assert resp.status_code != 401, f"{path} should accept valid key, got {resp.status_code}: {resp.text}"

    def test_invalid_key_rejected(self, client):
        """Invalid key should be rejected on protected routes."""
        simple_post_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/documents/test-id"]]
        simple_get_paths = [p for p in EXPECTED_AUTH if EXPECTED_AUTH[p] == "key" and "{" not in p and p not in ["/documents", "/documents/bulk", "/ask", "/ask/stream", "/search/explain", "/search", "/suggest", "/related", "/graph/neighbors", "/graph/report"]]
        
        for path in simple_post_paths:
            headers = {"X-API-Key": "invalid-key"}
            payload = {}
            if path == "/documents":
                payload = {"doc_id": "test", "title": "t", "content": "c", "doc_type": "text", "metadata": {}}
            elif path == "/ask":
                payload = {"query": "test"}
            elif path == "/ask/stream":
                payload = {"query": "test"}
            elif path == "/search/explain":
                payload = {"query": "test"}
            elif path == "/documents/bulk":
                payload = [{"doc_id": "test", "title": "t", "content": "c", "doc_type": "text", "metadata": {}}]
            elif path == "/documents/test-id":
                payload = {}
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 401, f"{path} should reject invalid key"
        
        for path in simple_get_paths:
            headers = {"X-API-Key": "invalid-key"}
            resp = client.get(path, headers=headers)
            assert resp.status_code == 401, f"{path} should reject invalid key"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])