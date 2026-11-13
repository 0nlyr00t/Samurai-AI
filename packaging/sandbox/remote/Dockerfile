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
# Base is Debian trixie (slim), pinned by its multi-arch index digest. trixie is
# the first Debian with FreeRDP 3 (freerdp3-x11 → /usr/bin/xfreerdp3) for both
# amd64 and arm64; bookworm only ships FreeRDP 2, which does not honour the
# exit-code/ERRCONNECT contract remote.rdp-auth relies on. The belt is installed
# at build time, so a rebuild changes the image digest — ci re-pins the profile
# (ops-image.yml).
FROM debian@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

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
