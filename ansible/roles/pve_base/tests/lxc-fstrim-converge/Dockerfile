# Image for test-lxc-fstrim-converge-docker.sh: Debian trixie (the Proxmox 9 base) with systemd as PID 1,
# the tools a Proxmox host has that the trim uses (e2fsprogs, util-linux), and the ansible bundle the
# repo's converge tests pin (ansible 14.0.0, as pve_ec_fan's).
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates e2fsprogs util-linux procps kmod \
      python3 python3-venv systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
