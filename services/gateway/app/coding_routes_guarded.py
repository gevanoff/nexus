from __future__ import annotations

from app import coding_agent_guarded as guarded_agent
from app import coding_routes as routes
from app.coding_semantic_memory import start_runtime as start_semantic_memory
from app.coding_semantic_memory import stop_runtime as stop_semantic_memory


# Route handlers resolve their module-level controller dependency at call time.
# Bind that dependency to the explicit reconciliation facade before exporting
# the existing router so authentication, request models, and response contracts
# remain unchanged.
routes.ca = guarded_agent
if start_semantic_memory not in routes.router.on_startup:
    routes.router.on_startup.append(start_semantic_memory)
if stop_semantic_memory not in routes.router.on_shutdown:
    routes.router.on_shutdown.append(stop_semantic_memory)
router = routes.router
