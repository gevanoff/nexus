# HeartMula Service

Containerized HeartMula shim exposing `POST /v1/audio/generations`.

The container is Nexus-owned. The actual model runtime still depends on the upstream HeartMuLa repo, which can be cloned into the persistent data volume on startup.

## Runtime

- Recommended host: `ada2`
- Default port: `9185`

## Performance note

No clear container-specific performance regression is expected on Linux/NVIDIA. The main risks remain upstream CUDA, audio dependency, and repo-runtime compatibility.