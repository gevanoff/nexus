# SkyReels V2 Service

Containerized video-generation shim exposing `POST /v1/videos/generations`.

The container is Nexus-owned. The actual model runtime still depends on the upstream SkyReels-V2 repo, which can be cloned into the persistent data volume on startup.

## Runtime

- Recommended host: `ada2`
- Default port: `9180`

## Performance note

No clear container-specific performance regression is expected on Linux/NVIDIA. The main risks remain upstream CUDA and driver compatibility.