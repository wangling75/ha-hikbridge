ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM alpine:3.20 AS go2rtc
ARG GO2RTC_VERSION=v1.9.14
ADD https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_amd64 /go2rtc
RUN chmod 0755 /go2rtc && /go2rtc -version

# A static FFmpeg keeps the image small and avoids Debian's libllvm/mesa
# dependency tree. go2rtc rejects the old 4.2 imageio build, so this is
# pinned to 7.0.2 and verified before it is copied into the final image.
FROM alpine:3.20 AS ffmpeg
ARG IMAGEIO_FFMPEG_VERSION=0.6.0
ARG IMAGEIO_FFMPEG_SHA256=c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282
RUN apk add --no-cache curl unzip \
    && curl -fsSL -o /ffmpeg.whl \
        "https://files.pythonhosted.org/packages/a0/2d/43c8522a2038e9d0e7dbdf3a61195ecc31ca576fb1527a528c877e87d973/imageio_ffmpeg-${IMAGEIO_FFMPEG_VERSION}-py3-none-manylinux2014_x86_64.whl" \
    && echo "${IMAGEIO_FFMPEG_SHA256}  /ffmpeg.whl" | sha256sum -c - \
    && unzip -p /ffmpeg.whl imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2 > /ffmpeg \
    && chmod 0755 /ffmpeg

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

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=go2rtc /go2rtc /usr/local/bin/go2rtc
COPY hikbridge /opt/hikbridge/hikbridge
COPY hcnetsdk /opt/hikbridge/hcnetsdk
# Copied after the source tree so the downloaded bundle always wins.
COPY --from=hlsjs /hls.min.js /opt/hikbridge/hikbridge/hls.min.js
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/hikbridge

CMD [ "/run.sh" ]
