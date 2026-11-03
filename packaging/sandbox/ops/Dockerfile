# Operational toolbelt for remote admin, VMs and exploit work.
#
# Base is pinned by digest; the toolbelt itself is installed at build time, so a
# rebuild changes the image digest — re-pin the profile after a rebuild.
FROM debian@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        smbclient \
        freerdp2-x11 \
        adb \
        python3 \
        python3-pip \
        python3-pwntools \
        gcc \
        gdb \
        binutils \
        file \
        socat \
        xvfb \
        xauth \
        jq \
        curl \
        nmap \
        iproute2 \
        dnsutils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages impacket==0.12.0

# no interactive history, no default key prompts
ENV PYTHONUNBUFFERED=1
WORKDIR /work
