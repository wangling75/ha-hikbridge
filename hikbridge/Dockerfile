ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM python:3.12-slim AS ffmpeg
ARG PYPI_MIRROR=https://pypi.org/simple
RUN python3 -m pip install --no-cache-dir --index-url "${PYPI_MIRROR}" imageio-ffmpeg==0.5.1 \
    && cp "$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" /usr/local/bin/ffmpeg \
    && chmod +x /usr/local/bin/ffmpeg

FROM alpine:3.20 AS go2rtc
ARG GO2RTC_VERSION=v1.9.14
ADD https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_amd64 /go2rtc
RUN chmod 0755 /go2rtc && /go2rtc -version

# hls.js lets Chrome/Firefox play the HLS stream the same way Home
# Assistant's own camera cards do remotely. Safari plays HLS natively and
# does not need it, so a failed download only disables that one path.
FROM alpine:3.20 AS hlsjs
ARG HLSJS_VERSION=1.5.17
RUN apk add --no-cache curl \
    && (curl -fsSL -o /hls.min.js \
          "https://cdn.jsdelivr.net/npm/hls.js@${HLSJS_VERSION}/dist/hls.min.js" \
        || curl -fsSL -o /hls.min.js \
          "https://unpkg.com/hls.js@${HLSJS_VERSION}/dist/hls.min.js") \
    && test "$(wc -c < /hls.min.js)" -gt 100000 \
    && grep -qi "hls.js" /hls.min.js

FROM ${BUILD_FROM}

ARG APT_MIRROR=deb.debian.org

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HIK_SDK_DIR=/opt/hikbridge/hcnetsdk \
    ALSA_CONFIG_PATH=/opt/hikbridge/hikbridge/asound.conf \
    LD_LIBRARY_PATH=/opt/hikbridge/hcnetsdk:/opt/hikbridge/hcnetsdk/HCNetSDKCom

# libasound2 is required because the SDK's intercom plugin brings up OpenAL,
# which refuses to initialise without a playback device. A null PCM device is
# enough and is supplied through ALSA_CONFIG_PATH (see asound.conf).
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do \
        [ -f "$f" ] && sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f"; \
    done \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 libasound2 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=go2rtc /go2rtc /usr/local/bin/go2rtc
COPY hikbridge /opt/hikbridge/hikbridge
COPY hcnetsdk /opt/hikbridge/hcnetsdk
# Copied after the source tree so the downloaded bundle always wins.
COPY --from=hlsjs /hls.min.js /opt/hikbridge/hikbridge/hls.min.js
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/hikbridge

CMD [ "/run.sh" ]
