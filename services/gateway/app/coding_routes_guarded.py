from __future__ import annotations

from app import coding_agent_guarded as guarded_agent
from app import coding_routes as routes


# Route handlers resolve their module-level controller dependency at call time.
# Bind that dependency to the explicit reconciliation facade before exporting
# the existing router so authentication, request models, and response contracts
# remain unchanged.
routes.ca = guarded_agent
router = routes.router
