# Nexus Coding Smoke Routing Fixture

This fixture is a moderate Coding smoke test. The expected task is:

1. Fix `feature_flags.enabled_features` and `router.resolve_route`.
2. Run:

   ```bash
   python -m unittest discover -s fixtures/coding-smoke-routing -p "verify_*.py"
   git diff --check
   ```

The implementation should parse common truthy and falsy feature flag values,
normalize route paths, and return `not_found` for missing routes. The smoke
harness allows edits to `feature_flags.py` and `router.py` only.
