ARG BUILD_FROM=debian:bookworm-slim
FROM ${BUILD_FROM}

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    HIK_SDK_DIR=/opt/hikbridge/hcnetsdk \
    ALSA_CONFIG_PATH=/opt/hikbridge/hikbridge/asound.conf \
    LD_LIBRARY_PATH=/opt/hikbridge/hcnetsdk:/opt/hikbridge/hcnetsdk/HCNetSDKCom

# libasound2 is required because the SDK's intercom plugin brings up OpenAL,
# which refuses to initialise without a playback device. A null PCM device is
# enough and is supplied through ALSA_CONFIG_PATH (see asound.conf).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY hikbridge /opt/hikbridge/hikbridge
COPY hcnetsdk /opt/hikbridge/hcnetsdk
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/hikbridge

CMD [ "/run.sh" ]
