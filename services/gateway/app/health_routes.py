from __future__ import annotations

from fastapi import APIRouter, Request

from app.auth import require_bearer


router = APIRouter()


@router.get("/v1/gateway/status")
async def gateway_status(req: Request):
    """Get gateway status including backend health and admission control stats."""
    require_bearer(req)
    
    from app.backends import get_admission_controller
    from app.health_checker import get_health_checker
    
    admission = get_admission_controller()
    health = get_health_checker()
    
    # Get admission control stats
    admission_stats = admission.get_stats()
    
    # Get health status for all backends
    health_status = {}
    for backend_class, status in health.get_all_status().items():
        health_status[backend_class] = {
            "healthy": status.is_healthy,
            "ready": status.is_ready,
            "last_check": status.last_check,
            "error": status.error,
        }
    
    return {
        "admission_control": admission_stats,
        "backend_health": health_status,
    }
