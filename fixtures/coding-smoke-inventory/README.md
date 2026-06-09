# Nexus Coding Smoke Inventory Fixture

This fixture is a medium-complexity Coding smoke test. The expected task is:

1. Fix `inventory_tools.build_reorder_plan`.
2. Run:

   ```bash
   python -m unittest discover -s fixtures/coding-smoke-inventory -p "verify_*.py"
   git diff --check
   ```

The implementation should normalize SKU strings, aggregate duplicate rows, and
return only sorted positive reorder quantities. The smoke harness fails the run
if the agent edits `verify_behavior.py` or any file outside the allowed
implementation path.
