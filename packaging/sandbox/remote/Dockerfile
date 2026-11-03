# Remote administration belt: ssh, smb, adb plus the utilities an operator needs
# while inside a target. The compiler lives in the exploit belt, so an admin
# session does not carry gcc and gdb.
#
# No freerdp here: the RDP probe is a stdlib X.224/TLS driver, and freerdp alone
# would drag in xvfb, mesa and llvm (~180 MB) for a check that never opens a
# session. Interactive RDP work needs a separate belt.
#
# Base is pinned by digest; the belt itself is installed at build time, so a
# rebuild changes the image digest — ci re-pins the profile (ops-image.yml).
FROM debian@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        smbclient \
        adb \
        python3 \
        socat \
        nmap \
        curl \
        jq \
        iproute2 \
        dnsutils \
        ca-certificates \
        ncurses-base \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

# the remote drivers are python: they own the tunnel and hand the endpoint out
# there is no real terminal in here; a dumb one keeps tools from warning
ENV PYTHONUNBUFFERED=1 \
    TERM=dumb
WORKDIR /work
