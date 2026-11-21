# Image for ../test-nfs-disk-gate-converge-docker.sh: Debian trixie (the Proxmox 9 base) with systemd as PID 1 and the
# ansible bundle the repo's converge tests pin (ansible 14.0.0). PVE itself is not installed: converge.sh puts a
# stand-in pve-guests.service, storage.cfg, VM configs and a `pvesm` stub in place.
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates util-linux procps \
      python3 python3-venv systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
