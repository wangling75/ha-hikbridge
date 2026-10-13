ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM python:3.12-slim AS ffmpeg
ARG PYPI_MIRROR=https://pypi.org/simple
RUN python3 -m pip install --no-cache-dir --index-url "${PYPI_MIRROR}" imageio-ffmpeg==0.5.1 \
    && cp "$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" /usr/local/bin/ffmpeg \
    && chmod +x /usr/local/bin/ffmpeg

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
COPY hikbridge /opt/hikbridge/hikbridge
COPY hcnetsdk /opt/hikbridge/hcnetsdk
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/hikbridge

CMD [ "/run.sh" ]
