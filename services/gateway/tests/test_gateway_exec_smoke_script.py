from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_smoke_module():
    root = Path(__file__).resolve().parents[3]
    spec = spec_from_file_location(
        "smoke_gateway_exec_tools",
        root / "services" / "gateway" / "tools" / "smoke_gateway_exec_tools.py",
    )
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_non_streaming_smoke_treats_malformed_success_body_as_failure():
    smoke = load_smoke_module()

    assert smoke.final_answer_seen(200, "<html>proxy error</html>", stream=False) is False


def test_streaming_smoke_requires_success_status_and_done_event():
    smoke = load_smoke_module()
    body = 'data: {"object":"chat.completion.chunk"}\n\ndata: [DONE]\n\n'

    assert smoke.final_answer_seen(200, body, stream=True) is True
    assert smoke.final_answer_seen(503, body, stream=True) is False
