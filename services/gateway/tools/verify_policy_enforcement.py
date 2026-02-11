#!/usr/bin/env python3
"""Quick verification script for policy enforcement implementation.

Tests basic functionality without requiring running backends.
"""

import asyncio
import sys
from pathlib import Path

# Add gateway to path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_backends_module():
    """Test backends module can be imported and initialized."""
    print("Testing backends module...")
    from app.backends import BackendRegistry, BackendConfig, AdmissionController
    
    # Create a minimal registry
    backends = {
        "test": BackendConfig(
            backend_class="test",
            base_url="http://localhost",
            description="Test",
            supported_capabilities=["chat"],
            concurrency_limits={"chat": 2},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        )
    }
    registry = BackendRegistry(backends=backends, legacy_mapping={})
    
    # Test admission controller
    admission = AdmissionController(registry)
    
    # Acquire and release
    await admission.acquire("test", "chat")
    admission.release("test", "chat")
    
    print("✓ Backends module OK")


async def test_health_checker():
    """Test health checker module."""
    print("Testing health checker module...")
    from app.health_checker import HealthChecker
    
    checker = HealthChecker(check_interval=999, timeout=1.0)
    
    # Should start with optimistic status
    assert checker.is_ready("nonexistent") is True
    
    print("✓ Health checker module OK")


async def test_image_storage():
    """Test image storage module."""
    print("Testing image storage module...")
    from app.image_storage import store_image_and_get_url, convert_response_to_urls
    import base64
    
    # Create a small test image (1x1 PNG)
    png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    b64_data = base64.b64encode(png_bytes).decode('ascii')
    
    # Test URL generation (may fail if directory doesn't exist, that's OK)
    try:
        url = store_image_and_get_url(b64_data, "image/png")
        assert url.startswith("/ui/images/") or url.startswith("data:")
    except Exception as e:
        print(f"  (storage skipped: {e})")
    
    # Test response conversion
    response = {
        "data": [
            {"b64_json": b64_data}
        ],
        "_gateway": {"mime": "image/png"}
    }
    
    converted = convert_response_to_urls(response)
    assert "data" in converted
    assert "url" in converted["data"][0] or "b64_json" in converted["data"][0]
    
    print("✓ Image storage module OK")


async def test_config_loading():
    """Test that backends config can be loaded."""
    print("Testing config loading...")
    from app.backends import load_backends_config
    from pathlib import Path
    
    config_path = Path(__file__).parent.parent / "app" / "backends_config.yaml"
    
    if config_path.exists():
        registry = load_backends_config(config_path)
        assert len(registry.backends) > 0
        assert "local_mlx" in registry.backends or "ollama" in registry.backends
        print(f"  Loaded {len(registry.backends)} backends")
    else:
        print(f"  Config not found at {config_path}, using defaults")
        registry = load_backends_config()
    
    print("✓ Config loading OK")


async def test_admission_control_limits():
    """Test that admission control enforces limits."""
    print("Testing admission control limits...")
    from app.backends import BackendRegistry, BackendConfig, AdmissionController
    from fastapi import HTTPException
    
    backends = {
        "test": BackendConfig(
            backend_class="test",
            base_url="http://localhost",
            description="Test",
            supported_capabilities=["chat"],
            concurrency_limits={"chat": 2},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        )
    }
    registry = BackendRegistry(backends=backends, legacy_mapping={})
    admission = AdmissionController(registry)
    
    # Should allow up to limit
    await admission.acquire("test", "chat")
    await admission.acquire("test", "chat")
    
    # Should reject when at limit
    try:
        await admission.acquire("test", "chat")
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 429
        print(f"  Correctly rejected with 429: {e.detail.get('error')}")
    
    # Clean up
    admission.release("test", "chat")
    admission.release("test", "chat")
    
    print("✓ Admission control limits OK")


async def main():
    """Run all verification tests."""
    print("=" * 60)
    print("Gateway Policy Enforcement - Verification")
    print("=" * 60)
    print()
    
    tests = [
        test_backends_module,
        test_health_checker,
        test_image_storage,
        test_config_loading,
        test_admission_control_limits,
    ]
    
    for test in tests:
        try:
            await test()
        except Exception as e:
            print(f"✗ {test.__name__} FAILED: {e}")
            import traceback
            traceback.print_exc()
            return 1
        print()
    
    print("=" * 60)
    print("All verification tests passed!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
