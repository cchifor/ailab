# Image for ../test-thin-textfile-converge-docker.sh: Debian trixie (the Proxmox 9 base) with systemd as PID 1,
# LVM with thin provisioning (a real thin pool on a loop device stands in for pve/data), and the ansible
# bundle the repo's converge tests pin (ansible 14.0.0).
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates lvm2 thin-provisioning-tools util-linux procps kmod \
      python3 python3-venv systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0 \
 && sed -i -E 's/^([[:space:]]*)# (udev_sync|udev_rules) = 1/\1\2 = 0/' /etc/lvm/lvm.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
