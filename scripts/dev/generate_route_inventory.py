#!/usr/bin/env python3
"""Generate route inventory from the FastAPI app object."""
import os
import json

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_API_KEY", "dev-key")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.api import app

def get_route_info():
    routes = []
    for route in app.routes:
        if hasattr(route, 'methods'):
            methods = sorted(list(route.methods))
        else:
            methods = ['N/A']
        routes.append({
            "path": route.path,
            "methods": methods,
            "name": getattr(route, 'name', 'unnamed'),
        })
    return routes

if __name__ == "__main__":
    routes = get_route_info()
    print(json.dumps(routes, indent=2))