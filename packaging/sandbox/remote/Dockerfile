# Remote administration belt: ssh, smb, rdp, adb plus the utilities an operator
# needs while inside a target. The compiler lives in the exploit belt, so an
# admin session does not carry gcc and gdb.
#
# Base is pinned by digest; the belt itself is installed at build time, so a
# rebuild changes the image digest — ci re-pins the profile (ops-image.yml).
FROM debian@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        smbclient \
        freerdp2-x11 \
        xvfb \
        xauth \
        adb \
        socat \
        nmap \
        curl \
        jq \
        iproute2 \
        dnsutils \
        ca-certificates \
        ncurses-base \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

# there is no real terminal in here; a dumb one keeps tools from warning
ENV PYTHONUNBUFFERED=1 \
    TERM=dumb
WORKDIR /work
