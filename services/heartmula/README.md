# HeartMula Service

Containerized HeartMula service exposing `POST /v1/music/generations` and a compatibility alias at `POST /v1/audio/generations`.

The container is Nexus-owned. The upstream HeartMuLa repo is still cloned into the persistent data volume on startup, but the service now owns pipeline loading and generation directly instead of relying on an operator-supplied run command.

## Runtime

- Recommended host: `ada2`
- Default port: `9185`

## Performance note

No clear container-specific performance regression is expected on Linux/NVIDIA. The main risks remain upstream CUDA, audio dependency, and repo-runtime compatibility.