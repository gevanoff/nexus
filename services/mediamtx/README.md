# MediaMTX Service

Containerized MediaMTX streaming service for RTMP ingest with browser-consumable playback on Linux hosts.

This is the recommended Nexus component when you want one or more publishers to send RTMP into a host like `ai1`, while multiple downstream consumers read the same stream through HLS, WebRTC, RTSP, or RTMP.

## Runtime

- Recommended host: `ai1`
- RTMP ingest port: `1935`
- HLS playback port: `8888`
- WebRTC playback port: `8889`
- RTSP port: `8554`
- API port: `9997`

## Compose

Use [docker-compose.mediamtx.yml](../../docker-compose.mediamtx.yml).

The checked-in config enables:

- RTMP ingest
- HLS playback for browser compatibility
- WebRTC playback for lower-latency browser clients
- RTSP and RTMP read paths for non-browser consumers
- MediaMTX API for operational health and discovery

## Why MediaMTX here

For Nexus, MediaMTX is a better fit than nginx-rtmp when you already expect multiple consumers and may later surface streams through the gateway frontend.

- It supports multiple output protocols from the same incoming stream.
- It gives browsers a viable playback path without making RTMP itself the frontend protocol.
- It is easier to operate as a dedicated streaming component than layering media behavior onto nginx.

## Gateway and discovery integration

The compose stack registers MediaMTX in etcd as an operational service record named `mediamtx`.
That record is intended for operator visibility and future frontend discovery, not as a normal model backend.

- `MEDIAMTX_ADVERTISE_BASE_URL` should usually point to the browser-friendly playback root, for example `http://ai1:8888`.
- `MEDIAMTX_API_ADVERTISE_URL` should point to the API root, for example `http://ai1:9997`.
- Publishers can push to `rtmp://ai1:1935/live/<stream-key>`.
- HLS playback is available at `http://ai1:8888/live/<stream-key>/index.m3u8`.

## Example workflow

Publish from OBS or ffmpeg:

```bash
ffmpeg -re -stream_loop -1 -i sample.mp4 -c copy -f flv rtmp://ai1:1935/live/demo
```

Open the HLS output in a browser/player:

```bash
http://ai1:8888/live/demo/index.m3u8
```

## Config

The base config lives at [services/mediamtx/mediamtx.yml](mediamtx.yml).

It is intentionally conservative:

- one default `live` path namespace
- no recording by default
- API enabled for health/ops
- HLS and WebRTC enabled for future frontend use

If you later need auth, recording, or per-path controls, extend the config there rather than modifying the container image.