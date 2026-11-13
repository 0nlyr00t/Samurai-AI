# Remote administration belt: ssh, smb, adb plus the utilities an operator needs
# while inside a target. The compiler lives in the exploit belt, so an admin
# session does not carry gcc and gdb.
#
# The read-only RDP probe (remote.rdp-check) is a stdlib X.224/TLS driver and
# needs no freerdp. Credential validation (remote.rdp-auth) does: CredSSP/NLA is
# not something the stdlib can speak, so freerdp3-x11 is installed for its
# /auth-only logon. /auth-only opens no window, so the GL stack (mesa, llvm, the
# ~180 MB the check never justified) is deliberately left out; xvfb is the small
# fallback for builds that still demand a display, driven directly (not the racy
# xvfb-run) by the driver.
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
        freerdp3-x11 \
        xvfb \
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
