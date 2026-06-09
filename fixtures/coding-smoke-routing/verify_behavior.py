from __future__ import annotations

import unittest

from feature_flags import enabled_features
from router import build_routes, resolve_route


class RouteFlagTests(unittest.TestCase):
    def test_feature_flags_accept_common_truthy_values(self) -> None:
        self.assertEqual(
            enabled_features(
                {
                    "billing": "YES",
                    "reports": "true",
                    "search": 1,
                    "admin": "no",
                    "legacy": 0,
                    "beta": False,
                }
            ),
            ["billing", "reports", "search"],
        )

    def test_routes_include_enabled_features(self) -> None:
        routes = build_routes({"billing": "yes", "reports": True, "admin": "false"})

        self.assertEqual(routes["/"], "home")
        self.assertEqual(routes["/health"], "health")
        self.assertEqual(routes["/billing"], "billing")
        self.assertEqual(routes["/reports"], "reports")
        self.assertNotIn("/admin", routes)

    def test_resolve_route_normalizes_path_and_has_not_found(self) -> None:
        routes = build_routes({"billing": True})

        self.assertEqual(resolve_route(routes, "billing/"), "billing")
        self.assertEqual(resolve_route(routes, "/health/"), "health")
        self.assertEqual(resolve_route(routes, "missing"), "not_found")


if __name__ == "__main__":
    unittest.main()
