from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "app" / "music_main.py"
SPEC = importlib.util.spec_from_file_location("nexus_music_main", MODULE_PATH)
assert SPEC and SPEC.loader
music_main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(music_main)


def test_extracts_audio_path_from_current_ace_step_result_shape() -> None:
    payload = {
        "data": [
            {
                "result": (
                    '[{"file": "/v1/audio?path=%2Fdata%2Fapp%2F.cache%2Facestep'
                    '%2Ftmp%2Fapi_audio%2Fgenerated.wav", "status": 1}]'
                ),
                "status": 1,
            }
        ]
    }

    assert (
        music_main._first_audio_reference(payload)
        == "/data/app/.cache/acestep/tmp/api_audio/generated.wav"
    )


def test_preserves_direct_audio_url() -> None:
    payload = {"data": [{"audio_url": "http://ace-step:8001/output/generated.wav?download=1"}]}

    assert (
        music_main._first_audio_reference(payload)
        == "http://ace-step:8001/output/generated.wav?download=1"
    )
