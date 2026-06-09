from __future__ import annotations

from typing import Mapping

from feature_flags import enabled_features


def build_routes(settings: Mapping[str, object]) -> dict[str, str]:
    routes = {
        "/": "home",
        "/health": "health",
    }
    for feature in enabled_features(settings):
        routes[f"/{feature}"] = feature
    return routes


def resolve_route(routes: Mapping[str, str], path: str) -> str:
    return routes.get(path, "")
