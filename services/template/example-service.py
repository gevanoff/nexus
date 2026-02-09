"""
Example Nexus Service

This is a minimal example showing how to implement a Nexus-compatible service
with all required endpoints.
"""

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import os

# Configuration
SERVICE_NAME = os.getenv("SERVICE_NAME", "example-service")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "9000"))

# Initialize FastAPI
app = FastAPI(
    title=SERVICE_NAME,
    version=SERVICE_VERSION,
    description="Example Nexus service demonstrating required endpoints"
)


# ===== Required Endpoints =====

@app.get("/health")
async def health():
    """
    Liveness check - is the service process running?
    
    Returns:
        200: Service is alive
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    """
    Readiness check - can the service handle requests?
    
    Checks:
    - Service is initialized
    - Dependencies are available
    - Backend connections work
    
    Returns:
        200: Service is ready
        503: Service is not ready
    """
    checks = {
        "service_initialized": True,
        "dependencies": "ok",
        "backend": "ok"
    }
    
    # Add your readiness checks here
    # Example: database connection, model loaded, etc.
    
    all_ok = all(
        v == "ok" if isinstance(v, str) else v
        for v in checks.values()
    )
    
    status_code = 200 if all_ok else 503
    
    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks
    }


@app.get("/v1/metadata")
async def metadata():
    """
    Service discovery - advertise capabilities and endpoints
    
    This endpoint allows the gateway and other services to discover:
    - What this service does
    - What endpoints it exposes
    - What configuration options are available
    - What resources it needs
    
    Returns:
        Service metadata conforming to schema v1
    """
    return {
        "schema_version": "v1",
        "service": {
            "name": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "description": "Example service demonstrating Nexus API patterns"
        },
        "backend": {
            "name": "example-backend",
            "vendor": "Example Corp",
            "base_url": "http://localhost:9000"
        },
        "endpoints": [
            {
                "path": "/health",
                "method": "GET",
                "operation_id": "health.check",
                "summary": "Liveness check"
            },
            {
                "path": "/readyz",
                "method": "GET",
                "operation_id": "readiness.check",
                "summary": "Readiness check"
            },
            {
                "path": "/v1/metadata",
                "method": "GET",
                "operation_id": "metadata.get",
                "summary": "Service discovery"
            },
            {
                "path": "/v1/example/process",
                "method": "POST",
                "operation_id": "example.process",
                "summary": "Process example request"
            }
        ],
        "capabilities": {
            "domains": ["example"],
            "modalities": ["text"],
            "streaming": False,
            "max_concurrency": 5
        },
        "ui": {
            "options": [
                {
                    "key": "mode",
                    "label": "Processing Mode",
                    "type": "enum",
                    "values": ["fast", "accurate"],
                    "default": "fast",
                    "description": "Choose processing mode"
                },
                {
                    "key": "threshold",
                    "label": "Threshold",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "default": 0.5,
                    "description": "Processing threshold"
                }
            ]
        },
        "resources": {
            "cpu": "1",
            "memory": "1Gi",
            "gpu": "optional"
        }
    }


# ===== Service-Specific Endpoints =====

class ExampleRequest(BaseModel):
    """Example request model"""
    input: str
    mode: Optional[str] = "fast"
    threshold: Optional[float] = 0.5


class ExampleResponse(BaseModel):
    """Example response model"""
    output: str
    mode: str
    metadata: Dict[str, Any]


@app.post("/v1/example/process", response_model=ExampleResponse)
async def process_example(request: ExampleRequest):
    """
    Example endpoint demonstrating service-specific functionality
    
    This would be replaced with your actual service logic.
    """
    # Your service logic here
    result = f"Processed: {request.input}"
    
    return ExampleResponse(
        output=result,
        mode=request.mode,
        metadata={
            "threshold": request.threshold,
            "processing_time_ms": 42
        }
    )


@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "status": "running",
        "endpoints": {
            "health": "/health",
            "readiness": "/readyz",
            "metadata": "/v1/metadata",
            "docs": "/docs"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
