import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
SKYREELS_MAIN = REPO_ROOT / "services" / "skyreels-v2" / "app" / "main.py"
SKYREELS_DOCKERFILE = REPO_ROOT / "services" / "skyreels-v2" / "Dockerfile"


class _FakeFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda func: func

    def post(self, *args, **kwargs):
        return lambda func: func


class _FakeHTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FakeJSONResponse:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _install_fastapi_stub(monkeypatch):
    fastapi = ModuleType("fastapi")
    fastapi.FastAPI = _FakeFastAPI
    fastapi.HTTPException = _FakeHTTPException
    fastapi.Request = object

    responses = ModuleType("fastapi.responses")
    responses.FileResponse = object
    responses.JSONResponse = _FakeJSONResponse

    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)


def _load_skyreels_main(monkeypatch):
    _install_fastapi_stub(monkeypatch)
    spec = importlib.util.spec_from_file_location("skyreels_v2_main_for_test", SKYREELS_MAIN)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cuda_probe_reports_child_process_failure(monkeypatch):
    module = _load_skyreels_main(monkeypatch)
    monkeypatch.setenv("SKYREELS_CUDA_PROBE_CACHE_TTL_SEC", "0")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="RuntimeError: No CUDA GPUs are available",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    error = module._cuda_probe_error()

    assert error is not None
    assert error["reason"] == "cuda_unavailable"
    assert "No CUDA GPUs are available" in error["detail"]


def test_cuda_probe_accepts_successful_child_process(monkeypatch):
    module = _load_skyreels_main(monkeypatch)
    monkeypatch.setenv("SKYREELS_CUDA_PROBE_CACHE_TTL_SEC", "0")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert module._cuda_probe_error() is None


def test_skyreels_container_healthcheck_uses_readiness():
    dockerfile = SKYREELS_DOCKERFILE.read_text(encoding="utf-8")
    healthcheck = next(line for line in dockerfile.splitlines() if "urllib.request.urlopen" in line)

    assert "/readyz" in healthcheck
    assert "/healthz" not in healthcheck
