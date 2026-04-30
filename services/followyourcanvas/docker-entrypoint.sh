#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${FYC_DATA_DIR:-/data}"
APP_DIR="${FYC_APP_DIR:-${DATA_DIR}/app}"
REPO_URL="${FYC_REPO_URL:-https://github.com/mayuelala/FollowYourCanvas.git}"
REPO_REF="${FYC_REPO_REF:-}"
UPDATE_ON_START="${FYC_UPDATE_ON_START:-false}"
REQ_HASH_FILE="${DATA_DIR}/.fyc-requirements.sha256"
DEFAULT_CONFIG="${FYC_DEFAULT_CONFIG:-infer-configs/prompt-panda-nexus.yaml}"

fyc_runtime_ready() {
  PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - <<'PY' >/dev/null 2>&1
import importlib
modules = (
    "torch",
    "diffusers",
    "transformers",
    "omegaconf",
    "decord",
    "segment_anything",
    "einops",
    "matplotlib",
    "cv2",
    "imageio_ffmpeg",
)
for name in modules:
    importlib.import_module(name)
PY
}

mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs"

if [[ -n "${REPO_URL}" ]]; then
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    git clone "${REPO_URL}" "${APP_DIR}" || true
  elif [[ "${UPDATE_ON_START,,}" == "true" ]]; then
    git -C "${APP_DIR}" fetch --all --tags || true
    git -C "${APP_DIR}" pull --ff-only || true
  fi

  if [[ -n "${REPO_REF}" && -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" fetch --all --tags || true
    git -C "${APP_DIR}" checkout "${REPO_REF}" || true
  fi

  if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    req_hash="$(sha256sum "${APP_DIR}/requirements.txt" | awk '{print $1}')"
    prev_hash="$(cat "${REQ_HASH_FILE}" 2>/dev/null || true)"
    if [[ "${req_hash}" != "${prev_hash}" ]] || ! fyc_runtime_ready; then
      python3 -m pip install --no-cache-dir -r "${APP_DIR}/requirements.txt"
      printf '%s' "${req_hash}" > "${REQ_HASH_FILE}"
    fi
  fi

  if [[ "${DEFAULT_CONFIG}" == "infer-configs/prompt-panda-nexus.yaml" ]]; then
    mkdir -p "${APP_DIR}/infer-configs"
    if [[ ! -f "${APP_DIR}/${DEFAULT_CONFIG}" ]]; then
      cat > "${APP_DIR}/${DEFAULT_CONFIG}" <<'YAML'
output_dir: "infer"
pretrained_model_path: "pretrained_models/stable-diffusion-2-1"
motion_pretrained_model_path: "pretrained_models/follow-your-canvas/checkpoint-40000.ckpt"
lmm_path: "pretrained_models/Qwen-VL-Chat"

unet_additional_kwargs:
  use_motion_module: true
  motion_module_resolutions: [1, 2, 4, 8]
  unet_use_cross_frame_attention: false
  unet_use_temporal_attention: false
  use_linear_projection: true
  use_inflated_groupnorm: true
  motion_module_mid_block: true
  use_fps_condition: true
  use_temporal_conv: false
  use_relative_postions: "WithAdapter"
  use_ip_plus_cross_attention: true
  ip_plus_condition: "video"
  num_tokens: 64
  use_adapter_temporal_projection: true
  compress_video_features: true
  image_hidden_size: 256
  use_outpaint: true
  motion_module_type: Vanilla
  motion_module_kwargs:
    num_attention_heads: 8
    num_transformer_block: 1
    attention_block_types: ["Temporal_Self", "Temporal_Self"]
    temporal_position_encoding: true
    temporal_position_encoding_max_len: 64
    temporal_attention_dim_div: 1
    zero_initialize: true

noise_scheduler_kwargs:
  num_train_timesteps: 1000
  beta_start: 0.00085
  beta_end: 0.012
  beta_schedule: "linear"
  steps_offset: 1
  clip_sample: false
  prediction_type: "v_prediction"
  rescale_betas_zero_snr: true

anchor_target_sampling:
  target_size:
    - 512
    - 512

validation_data:
  num_inference_steps: 40
  guidance_scale_text: 8.
  guidance_scale_adapter: -1
  multi_diff: true

video_dir: "demo_video/panda"
global_seed: -1
enable_xformers_memory_efficient_attention: true
use_fps_condition: true
prompts_input: ["a panda sitting on a grassy area in a lake, with forest mountain in the background"]
negative_prompt_input: ["noisy, ugly, nude, watermark"]
use_outpaint: true
use_ip_plus_cross_attention: true
ip_plus_condition: "video"
image_encoder_name: "SAM"
image_pretrained_model_path: "pretrained_models/sam/sam_vit_b_01ec64.pth"
target_size:
  - 1152
  - 2048
min_overlap:
  - 250
  - 250
YAML
    fi
  fi
fi

export FYC_WORKDIR="${FYC_WORKDIR:-${APP_DIR}}"
export FYC_DEFAULT_CONFIG="${DEFAULT_CONFIG}"
export PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${FYC_PORT:-9165}"
