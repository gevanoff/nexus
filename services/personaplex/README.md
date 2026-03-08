# PersonaPlex Service

Containerized PersonaPlex shim exposing `POST /v1/chat/completions`.

This port keeps the legacy `proxy` and `run command` modes, and adds optional startup bootstrap of the upstream `NVIDIA/personaplex` repo into the service data volume.

## Runtime

- Recommended host: `ada2`
- Default port: `9160`

## Notes

- The shim container is Nexus-owned.
- The upstream PersonaPlex runtime is still fetched from its source repo when `PERSONAPLEX_REPO_URL` is set.
- No intrinsic container performance issue is expected on Linux/NVIDIA; the main risk remains upstream runtime compatibility.