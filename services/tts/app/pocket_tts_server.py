"""FastAPI shim for Pocket TTS.

This server exposes a minimal OpenAI-compatible endpoint for text-to-speech:
- POST /v1/audio/speech
- GET /health
- GET /v1/models
"""
from __future__ import annotations

import base64
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Pocket TTS")


class SpeechRequest(BaseModel):
    input: str = Field(default=..., min_length=1)
    model: Optional[str] = None
    voice: Optional[str] = None
    response_format: str = Field(default="wav", pattern=r"^(wav|mp3)$")


class PocketTTSBackend:
    def __init__(self) -> None:
        self.backend = os.getenv("POCKET_TTS_BACKEND", "auto")
        self.command = os.getenv("POCKET_TTS_COMMAND", "pocket-tts")
        self.command_args = shlex.split(os.getenv("POCKET_TTS_COMMAND_ARGS", ""))
        self.text_arg = os.getenv("POCKET_TTS_COMMAND_TEXT_ARG", "--text")
        self.output_arg = os.getenv("POCKET_TTS_COMMAND_OUTPUT_ARG", "--output")
        self.model_arg = os.getenv("POCKET_TTS_COMMAND_MODEL_ARG", "--model")
        self.voice_arg = os.getenv("POCKET_TTS_COMMAND_VOICE_ARG", "--voice")
        self.format_arg = os.getenv("POCKET_TTS_COMMAND_FORMAT_ARG", "")
        self.model_path = os.getenv("POCKET_TTS_MODEL_PATH", "")
        self.default_voice = os.getenv("POCKET_TTS_VOICE", "alba")
        self.sample_rate = int(os.getenv("POCKET_TTS_SAMPLE_RATE", "22050"))
        self._python_backend: Optional[Any] = None

    def _load_python_backend(self) -> bool:
        try:
            import pocket_tts  # type: ignore
        except Exception:
            return False

        # Check for TTSModel API
        if hasattr(pocket_tts, "TTSModel"):
            try:
                self._python_backend = pocket_tts.TTSModel.load_model()
                return True
            except Exception:
                pass  # Fall through to generic API

        candidate = None
        if hasattr(pocket_tts, "PocketTTS"):
            candidate = pocket_tts.PocketTTS
        elif hasattr(pocket_tts, "TTS"):
            candidate = pocket_tts.TTS
        else:
            candidate = pocket_tts

        if candidate is pocket_tts:
            self._python_backend = pocket_tts
            return True

        try:
            if self.model_path:
                try:
                    self._python_backend = candidate(self.model_path)
                except TypeError:
                    self._python_backend = candidate(model_path=self.model_path)
            else:
                self._python_backend = candidate()
            return True
        except Exception:
            return False

    def _ensure_python_backend(self) -> bool:
        if self._python_backend is not None:
            return True
        return self._load_python_backend()

    def _python_synthesize(self, text: str, voice: str, response_format: str) -> bytes:
        backend = self._python_backend
        if backend is None:
            raise RuntimeError("python backend not loaded")

        # Check for TTSModel API
        if hasattr(backend, "generate_audio") and hasattr(backend, "get_state_for_audio_prompt"):
            if response_format != "wav":
                raise RuntimeError(f"TTSModel API only supports wav format, got {response_format}")
            try:
                voice_state = backend.get_state_for_audio_prompt(voice)
                audio_tensor = backend.generate_audio(voice_state, text)
                # Convert torch tensor to wav bytes
                import numpy as np
                import wave
                import io
                audio_np = audio_tensor.detach().cpu().numpy()
                # Scale float32 [-1, 1] to int16
                audio_int16 = (audio_np * 32767).astype(np.int16)
                # Assume 16-bit PCM, mono
                buffer = io.BytesIO()
                with wave.open(buffer, 'wb') as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(backend.sample_rate)
                    wav_file.writeframes(audio_int16.tobytes())
                return buffer.getvalue()
            except Exception as e:
                raise RuntimeError(f"TTSModel API failed: {e}")

        # Fallback to generic API
        for method_name in ("synthesize", "tts", "generate", "speak", "convert", "to_audio", "audio", "process", "create_audio", "__call__"):
            if hasattr(backend, method_name):
                method = getattr(backend, method_name)
                # Try different argument combinations
                arg_sets = [
                    (text,),  # simplest
                    (text, voice),  # text and voice
                    {"text": text, "voice": voice},  # kwargs
                    {"text": text, "voice": voice, "format": response_format},  # with format
                    {"text": text, "voice": voice, "model_path": self.model_path or None, "sample_rate": self.sample_rate, "response_format": response_format},  # full
                ]
                for args in arg_sets:
                    try:
                        if isinstance(args, dict):
                            result = method(**args)
                        else:
                            result = method(*args)
                        # Check various result types
                        if isinstance(result, bytes):
                            return result
                        if isinstance(result, tuple) and result:
                            maybe_audio = result[0]
                            if isinstance(maybe_audio, bytes):
                                return maybe_audio
                        if isinstance(result, str):
                            # Treat as file path
                            return Path(result).read_bytes()
                        if isinstance(result, dict):
                            for key in ("audio", "data", "output", "result"):
                                if key in result and isinstance(result[key], bytes):
                                    return result[key]
                                if key in result and isinstance(result[key], str):
                                    return Path(result[key]).read_bytes()
                        # Check for object with audio attribute
                        if hasattr(result, "audio") and isinstance(result.audio, bytes):
                            return result.audio
                        if hasattr(result, "data") and isinstance(result.data, bytes):
                            return result.data
                        if hasattr(result, "output") and isinstance(result.output, bytes):
                            return result.output
                    except (TypeError, AttributeError, KeyError):
                        continue
        raise RuntimeError("python backend did not expose a compatible synthesize method")

    def _command_synthesize(self, text: str, voice: str, response_format: str) -> bytes:
        suffix = "." + response_format
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            output_path = Path(tmp.name)

        cmd = [self.command] + self.command_args
        if self.text_arg:
            cmd += [self.text_arg, text]
        if self.output_arg:
            cmd += [self.output_arg, str(output_path)]
        if self.model_arg and self.model_path:
            cmd += [self.model_arg, self.model_path]
        if self.voice_arg and voice:
            cmd += [self.voice_arg, voice]
        if self.format_arg:
            cmd += [self.format_arg, response_format]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Pocket TTS command not found: {self.command}. Set POCKET_TTS_COMMAND to a valid binary."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Pocket TTS command failed: {exc.stderr.decode('utf-8', errors='ignore')}"
            ) from exc

        audio = output_path.read_bytes()
        output_path.unlink(missing_ok=True)
        return audio

    def synthesize(self, text: str, voice: str, response_format: str) -> bytes:
        backend_pref = self.backend.lower()
        if backend_pref not in {"auto", "python", "command"}:
            raise RuntimeError(f"Unsupported POCKET_TTS_BACKEND={self.backend}")

        if backend_pref == "python":
            if not self._ensure_python_backend():
                raise RuntimeError("POCKET_TTS_BACKEND=python but pocket_tts import failed")
            return self._python_synthesize(text, voice, response_format)
        elif backend_pref == "command":
            return self._command_synthesize(text, voice, response_format)
        else:  # auto
            if self._ensure_python_backend():
                try:
                    return self._python_synthesize(text, voice, response_format)
                except RuntimeError:
                    pass  # fall back to command
            return self._command_synthesize(text, voice, response_format)


backend = PocketTTSBackend()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    model_name = os.getenv("POCKET_TTS_MODEL_NAME", "pocket-tts")
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "owned_by": "pocket-tts",
            }
        ],
    }


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    voice = req.voice or os.getenv("POCKET_TTS_VOICE", "default")
    response_format = req.response_format

    try:
        audio = backend.synthesize(req.input, voice, response_format)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    media_type = "audio/wav" if response_format == "wav" else "audio/mpeg"
    return Response(content=audio, media_type=media_type)


@app.post("/v1/audio/speech/base64")
async def speech_base64(req: SpeechRequest) -> dict[str, str]:
    voice = req.voice or os.getenv("POCKET_TTS_VOICE", "default")
    response_format = req.response_format

    try:
        audio = backend.synthesize(req.input, voice, response_format)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    encoded = base64.b64encode(audio).decode("utf-8")
    return {"audio": encoded, "format": response_format}
