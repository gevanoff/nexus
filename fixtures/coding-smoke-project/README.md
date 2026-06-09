# Nexus Coding Smoke Project

This fixture is intentionally small so the Nexus Coding framework can be tested
end to end with an easy-to-audit task.

The expected task is:

1. Fix `math_tools.summarize_numbers` so it returns the correct median for
   even-length inputs.
2. Run:

   ```bash
   python -m unittest discover -s fixtures/coding-smoke-project -p "verify_*.py"
   git diff --check
   ```

The smoke harness should fail the run if the agent edits files outside this
fixture or changes `verify_behavior.py` instead of fixing the implementation.
