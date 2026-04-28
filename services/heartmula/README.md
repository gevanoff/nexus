# HeartMula Service

Containerized HeartMula service exposing `POST /v1/music/generations` and a compatibility alias at `POST /v1/audio/generations`.

The container is Nexus-owned. The upstream HeartMuLa repo is still cloned into the persistent data volume on startup, but the service now owns pipeline loading and generation directly instead of relying on an operator-supplied run command.

## Runtime

- Recommended host: `ada2`
- Default port: `9185`
- Checkpoint path: `/data/ckpt`
- Recommended traded-in setting: `HEARTMULA_LAZY_LOAD=true`

## Performance note

First production bring-up on `ada2` required downloading these upstream checkpoints into `.runtime/heartmula/ckpt`:

```bash
hf download --local-dir .runtime/heartmula/ckpt HeartMuLa/HeartMuLaGen
hf download --local-dir .runtime/heartmula/ckpt/HeartMuLa-oss-3B HeartMuLa/HeartMuLa-oss-3B-happy-new-year
hf download --local-dir .runtime/heartmula/ckpt/HeartCodec-oss HeartMuLa/HeartCodec-oss-20260123
```

Observed on `ada2`:

- idle VRAM with `HEARTMULA_LAZY_LOAD=true`: about 1GB after a request settles
- idle VRAM with eager load: about 22GB
- peak VRAM during a short generation smoke test: about 35GB
- first start spent several minutes installing upstream dependencies because `heartlib` is cloned and installed at container startup

The service adapter includes a `soundfile` fallback for current `torchaudio.save()` builds that require `torchcodec`.
