# InvokeAI Reference-Image Workflows

The Nexus InvokeAI shim exposes:

- `POST /v1/images/generations` for text-to-image
- `POST /v1/images/edits` for reference-image workflows

The edit endpoint accepts multipart form data.

## Request fields

| Field | Required | Notes |
|---|---:|---|
| `image` | yes | Reference image file |
| `prompt` | yes | Positive prompt |
| `purpose` | no | `image_to_image`, `composition`, `style`, or `controlnet`; defaults to `image_to_image` |
| `strength` | no | Influence from `0.0` to `1.0`; defaults to `0.65` |
| `mask` | no | Inpainting mask; valid only for `image_to_image` |
| `size` | no | Output size such as `1024x1024` |
| `n` | no | Number of outputs, 1–10 |
| `model` | no | InvokeAI model/preset identifier |
| `seed` | no | Deterministic seed; batches use consecutive seeds |
| `negative_prompt` | no | Negative prompt |
| `steps` | no | Denoising steps |
| `cfg_scale` / `guidance_scale` | no | Guidance scale |
| `scheduler` | no | Scheduler name |

The shim always returns OpenAI-style `data[].b64_json`; Gateway may convert those values to temporary URLs.

## Workflow template selection

Configure purpose-specific exported InvokeAI workflows or API graphs:

```bash
SHIM_IMG2IMG_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/img2img.json
SHIM_INPAINT_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/inpaint.json
SHIM_COMPOSITION_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/composition-ip-adapter.json
SHIM_STYLE_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/style-ip-adapter.json
SHIM_CONTROLNET_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/controlnet.json
```

Fallbacks:

```bash
SHIM_EDIT_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/generic-edit.json
SHIM_GRAPH_TEMPLATE_PATH=/var/lib/invokeai/workflows/text-to-image.json
```

Purpose-specific paths take precedence. The normal text-to-image template is only a final fallback and will fail validation unless it actually contains compatible reference nodes.

## Workflow validation

The shim changes only inputs already present in the selected graph. It rejects the request rather than silently ignoring unsupported controls.

Expected graph features:

### Image-to-image

- An `image_to_latents`, `img2img`, `image_to_image`, or similar source node with an image input.
- A strength input such as `strength`, `denoise_strength`, `denoising_strength`, or `denoising_start`.
- When a mask is uploaded, an inpainting/mask node exposing `mask`, `mask_image`, `image`, or `image_name`.

### Composition or style reference

- An IP-Adapter/reference-image node, such as `ip_adapter`, `flux_ip_adapter`, or `reference_image`.
- An image input and a weight/strength/scale input.
- Composition and style use different workflow templates because their adapter configuration is workflow-specific.

### ControlNet

- A `controlnet`, `control_adapter`, `controlnet_processor`, or T2I-Adapter node.
- A control-image input.
- A control weight/strength input.
- The graph owns preprocessor and ControlNet model selection.

## InvokeAI upload

Before queueing the workflow, the shim uploads the reference and optional mask to InvokeAI's image upload API and injects the returned `image_name` values into the graph.

## Gateway Image UI

Selecting a reference image in `/ui/image` changes the Generate action from `/ui/api/image` to `/ui/api/image/edit`. The browser provides:

- reference preview/removal
- purpose selection
- influence slider
- optional mask preview/removal
- client-side mask-dimension validation

Reference workflows currently target the InvokeAI backend (`gpu_heavy`). Text-to-image behavior remains unchanged when no reference image is selected.

## Example

```bash
curl -sS http://localhost:9091/v1/images/edits \
  -F 'image=@reference.png;type=image/png' \
  -F 'prompt=Turn this scene into a cinematic night exterior' \
  -F 'purpose=image_to_image' \
  -F 'strength=0.65' \
  -F 'size=1024x1024' \
  -F 'n=1'
```

Inpainting:

```bash
curl -sS http://localhost:9091/v1/images/edits \
  -F 'image=@source.png;type=image/png' \
  -F 'mask=@mask.png;type=image/png' \
  -F 'prompt=Replace the masked area with a stone archway' \
  -F 'purpose=image_to_image' \
  -F 'strength=0.8'
```
