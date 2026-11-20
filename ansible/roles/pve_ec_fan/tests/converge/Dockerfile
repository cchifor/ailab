# Image for test-converge-docker.sh: Debian trixie (the Proxmox 9 base) with the Proxmox kernel headers the
# hosts build against, DKMS, systemd as PID 1, the Debian node_exporter, and the ansible bundle the repo's
# converge tests pin (ansible 14.0.0). libelf and iproute2 are there on a Proxmox host; the build's objtool
# needs libelf.
FROM debian:trixie
ARG PVE_KERNEL=7.0.2-6-pve
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates curl \
 && curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
      -o /usr/share/keyrings/proxmox-archive-keyring.gpg \
 && echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c - \
 && printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/pve-no-subscription.sources \
 && apt-get update -q \
 && apt-get install -y -q --no-install-recommends dkms build-essential git kmod xz-utils openssl \
      "proxmox-headers-${PVE_KERNEL}" prometheus-node-exporter libelf1t64 iproute2 procps \
      python3 python3-venv python3-apt systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
