#!/usr/bin/env python3
# pyright: reportMissingImports=false

import base64
import json
import os
import sys
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_image_bytes(request_payload: Dict[str, Any], input_path: Optional[Path]) -> bytes:
    if input_path is not None:
        if input_path.suffix == ".url":
            url = input_path.read_text(encoding="utf-8").strip()
            if not url:
                raise ValueError("LIGHTON_OCR_INPUT_PATH .url file is empty")
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LightOnOCR/1.0; +https://github.com/gevanoff/nexus)",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        return input_path.read_bytes()

    image_b64 = request_payload.get("image")
    if image_b64:
        try:
            return base64.b64decode(image_b64)
        except Exception as exc:
            raise ValueError(f"Invalid base64 image: {exc}") from exc

    image_url = request_payload.get("image_url")
    if image_url:
        url_str = str(image_url).strip()
        if "…" in url_str or "\u2026" in url_str:
            raise ValueError("image_url contains an ellipsis (…). Provide a full URL.")
        try:
            url_str.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("image_url must be ASCII. Provide a fully-qualified URL without unicode characters.") from exc
        req = urllib.request.Request(
            url_str,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LightOnOCR/1.0; +https://github.com/gevanoff/nexus)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    raise ValueError("No image input provided")


def _load_image_pil(image_bytes: bytes):
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"Pillow is required for OCR: {exc}") from exc
    try:
        return Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise RuntimeError(f"Failed to decode image: {exc}") from exc


def _select_device() -> str:
    device = (_env("LIGHTON_OCR_DEVICE", "auto") or "auto").lower()
    if device not in {"auto", "cpu", "cuda", "mps"}:
        return "auto"
    return device


def _select_device_from_request(request_payload: Dict[str, Any]) -> str:
    raw = request_payload.get("device")
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"auto", "cpu", "cuda", "mps"}:
            return value
    return _select_device()


def _resolve_device(request_payload: Optional[Dict[str, Any]] = None) -> str:
    device = _select_device_from_request(request_payload or {})
    if device == "cpu":
        return "cpu"
    if device == "cuda":
        return "cuda"
    if device == "mps":
        return "mps"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _pipeline_device_arg(device: str) -> Any:
    if device == "cpu":
        return -1
    if device == "cuda":
        return 0
    if device == "mps":
        return "mps"
    return -1


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _supported_pipeline_tasks() -> list[str]:
    try:
        from transformers.pipelines import PIPELINE_REGISTRY
        return sorted(list(PIPELINE_REGISTRY.get_supported_tasks()))
    except Exception:
        return []


def _pick_task(request_payload: Dict[str, Any]) -> list[str]:
    raw = request_payload.get("task") or request_payload.get("operation")
    if isinstance(raw, str) and raw.strip():
        value = raw.strip()
        if value.lower() == "auto":
            return ["image-text-to-text", "image-to-text"]
        return [value]

    env_task = (_env("LIGHTON_OCR_TASK") or "").strip()
    if env_task:
        if env_task.lower() == "auto":
            return ["image-text-to-text", "image-to-text"]
        return [env_task]

    return ["image-text-to-text", "image-to-text"]


def _should_use_lighton_native(model_id: str, request_payload: Dict[str, Any]) -> bool:
    model_id_l = (model_id or "").lower()
    if "lightonocr" not in model_id_l:
        return False

    raw = request_payload.get("task") or request_payload.get("operation")
    if isinstance(raw, str) and raw.strip():
        task = raw.strip().lower()
        if task in {"image-text-to-text", "ocr", "auto"}:
            return True
    return True


def _run_lighton_native(image_path: str, request_payload: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    try:
        import torch
        from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor
    except Exception as exc:
        raise RuntimeError(f"LightOnOCR native path requires transformers>=5 and torch: {exc}") from exc

    max_tokens = _int_env("LIGHTON_OCR_MAX_TOKENS", 256)
    device = _resolve_device(request_payload)

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    if device == "mps" and not (getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
        device = "cpu"

    dtype = torch.bfloat16 if device == "cuda" else torch.float32

    prompt = request_payload.get("prompt") or request_payload.get("text")
    prompt_str = prompt.strip() if isinstance(prompt, str) else ""

    conversation_content: list[dict[str, Any]] = [{"type": "image", "url": image_path}]
    if prompt_str:
        conversation_content.append({"type": "text", "text": prompt_str})
    conversation = [{"role": "user", "content": conversation_content}]

    processor = LightOnOcrProcessor.from_pretrained(model_id)
    model = LightOnOcrForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype).to(device)

    inputs = processor.apply_chat_template(
        conversation,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
    inputs = {k: (v.to(device=device, dtype=dtype) if v.is_floating_point() else v.to(device)) for k, v in inputs.items()}

    generate_kwargs: Dict[str, Any] = {}
    if isinstance(request_payload.get("parameters"), dict):
        generate_kwargs.update(request_payload["parameters"])
    generate_kwargs.setdefault("max_new_tokens", max_tokens)

    try:
        output_ids = model.generate(**inputs, **generate_kwargs)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if device != "cpu" and ("out of memory" in msg.lower() or ("cuda" in msg.lower() and "memory" in msg.lower())):
            try:
                sys.stderr.write("LightOnOCR: CUDA/MPS memory issue; retrying native generate on CPU\n")
            except Exception:
                pass
            device = "cpu"
            dtype = torch.float32
            model = LightOnOcrForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype).to(device)
            inputs = {k: (v.to(device=device, dtype=dtype) if v.is_floating_point() else v.to(device)) for k, v in inputs.items()}
            output_ids = model.generate(**inputs, **generate_kwargs)
        else:
            raise

    generated_ids = output_ids[0, inputs["input_ids"].shape[1]:]
    output_text = processor.decode(generated_ids, skip_special_tokens=True)

    return {
        "text": output_text,
        "model": model_id,
        "data": [{"text": output_text}],
        "raw": {"output_text": output_text},
        "backend": "lighton-native",
        "device": device,
        "dtype": str(dtype).replace("torch.", ""),
    }


def _run_ocr(image, request_payload: Dict[str, Any]) -> Dict[str, Any]:
    model_id = _env("LIGHTON_OCR_MODEL_ID", "lightonai/LightOnOCR-2-1B")
    max_tokens = _int_env("LIGHTON_OCR_MAX_TOKENS", 256)
    device = _resolve_device(request_payload)

    try:
        from transformers import pipeline
    except Exception as exc:
        raise RuntimeError(f"transformers is required for OCR: {exc}") from exc

    if request_payload.get("list_tasks") is True:
        return {"tasks": _supported_pipeline_tasks(), "model": model_id}

    image_path = request_payload.get("_image_path")
    if isinstance(image_path, str) and image_path.strip() and _should_use_lighton_native(model_id, request_payload):
        return _run_lighton_native(image_path.strip(), request_payload, model_id)

    if image is None:
        raise ValueError("No image input provided")

    trust_remote_code = _bool_env("LIGHTON_OCR_TRUST_REMOTE_CODE", default=str(model_id).startswith("lightonai/"))

    last_exc: Optional[BaseException] = None
    pipe = None
    selected_task: Optional[str] = None
    task_candidates = _pick_task(request_payload)
    for task in task_candidates:
        try:
            pipe = pipeline(task, model=model_id, trust_remote_code=trust_remote_code, device=_pipeline_device_arg(device))
            selected_task = task
            break
        except KeyError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if device != "cpu" and ("out of memory" in msg.lower() or ("cuda" in msg.lower() and "memory" in msg.lower())):
                try:
                    sys.stderr.write("LightOnOCR: CUDA/MPS memory issue; retrying pipeline on CPU\n")
                except Exception:
                    pass
                device = "cpu"
                try:
                    pipe = pipeline(task, model=model_id, trust_remote_code=trust_remote_code, device=_pipeline_device_arg(device))
                    selected_task = task
                    break
                except Exception as exc2:
                    last_exc = exc2
                    continue
            last_exc = exc
            continue

    if pipe is None:
        tasks = _supported_pipeline_tasks()
        hint = f"; supported tasks: {tasks}" if tasks else ""
        raise RuntimeError(f"No usable pipeline task found (tried {task_candidates}): {last_exc}{hint}")

    if "inputs" in request_payload:
        inputs: Any = request_payload.get("inputs")
    else:
        prompt = request_payload.get("prompt") or request_payload.get("text")
        prompt_str = prompt.strip() if isinstance(prompt, str) else ""
        if (selected_task or "").strip().lower() == "image-text-to-text" and not prompt_str:
            prompt_str = (_env("LIGHTON_OCR_DEFAULT_PROMPT") or "Read the text in this image.").strip()
        if prompt_str:
            if (selected_task or "").strip().lower() == "image-text-to-text":
                inputs = {"images": image, "text": prompt_str}
            else:
                inputs = {"image": image, "text": prompt_str}
        else:
            inputs = image

    params: Dict[str, Any] = {}
    if isinstance(request_payload.get("parameters"), dict):
        params.update(request_payload["parameters"])
    params.setdefault("max_new_tokens", max_tokens)

    result = pipe(inputs, **params)
    text = None
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            text = first.get("generated_text") or first.get("text")
    if not text:
        text = str(result)

    return {"text": text, "model": model_id, "data": [{"text": text}], "raw": result}


def main() -> int:
    request_path = _env("LIGHTON_OCR_REQUEST_JSON")
    output_path = _env("LIGHTON_OCR_OUTPUT_JSON")
    input_path = _env("LIGHTON_OCR_INPUT_PATH")

    if not request_path or not output_path:
        sys.stderr.write("Missing LIGHTON_OCR_REQUEST_JSON or LIGHTON_OCR_OUTPUT_JSON\n")
        return 2

    request_payload = _read_json(Path(request_path))
    if request_payload.get("list_tasks") is True:
        response = _run_ocr(image=None, request_payload=request_payload)
        _write_json(Path(output_path), response)
        return 0

    image_bytes = _load_image_bytes(request_payload, Path(input_path) if input_path else None)

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(prefix="lightonocr_", suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp.flush()
            tmp_path = tmp.name
        request_payload["_image_path"] = tmp_path
        image = _load_image_pil(image_bytes)
        response = _run_ocr(image, request_payload)
        _write_json(Path(output_path), response)
        return 0
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())