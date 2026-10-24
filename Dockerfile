ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG SUBIQUITY_VERSION

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    python3 \
    python3-aiohttp \
    python3-apport \
    python3-attr \
    python3-bson \
    python3-debian \
    python3-distro-info \
    python3-jsonschema \
    python3-more-itertools \
    python3-packaging \
    python3-passlib \
    python3-pyroute2 \
    python3-pyudev \
    python3-requests \
    python3-requests-unixsocket \
    python3-systemd \
    python3-urwid \
    python3-yaml \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Subiquity dry-run timezone validation helper
RUN mkdir -p /run/systemd/system && \
    printf '#!/bin/sh\nif [ "$1" = "list-timezones" ]; then\n    find -L /usr/share/zoneinfo -type f -not -name "*.tab" -not -name "*.list" -not -name "*.zi" | sed "s|^/usr/share/zoneinfo/||" | sort\n    exit 0\nfi\nexit 0\n' > /usr/local/bin/timedatectl && \
    chmod +x /usr/local/bin/timedatectl

WORKDIR /subiquity

RUN git clone --depth 1 --branch ${SUBIQUITY_VERSION} https://github.com/canonical/subiquity.git . && \
    ./scripts/update-part.py curtin && \
    ./scripts/update-part.py probert && \
    rm -rf .git curtin/.git probert/.git

CMD ["python3", "scripts/validate-autoinstall-user-data.py"]
