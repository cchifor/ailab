# Image for ../test-thin-textfile-converge-docker.sh: Debian trixie (the Proxmox 9 base) with systemd as PID 1, LVM
# (a real VG with a linear LV on a loop device checks the lvs output format and the unit's hardening; Docker
# Desktop's kernel has no dm-thin-pool, so the thin numbers come from a stub listing), and the ansible bundle the
# repo's converge tests pin (ansible 14.0.0).
# LVM in this --privileged container can see the Docker host's block devices. global_filter makes it see loop
# devices only, so a VG on the Docker host (a Proxmox box's `pve`, say) is invisible here and can be neither
# found nor touched; the test also uses a VG name of its own and removes only what it created.
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates lvm2 util-linux procps kmod \
      python3 python3-venv systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0 \
 && sed -i -E 's/^([[:space:]]*)# (udev_sync|udev_rules) = 1/\1\2 = 0/' /etc/lvm/lvm.conf \
 && sed -i -E '0,/^devices \{/s//devices {\n\tglobal_filter = [ "a|^\/dev\/loop[0-9]+$|", "r|.*|" ]/' /etc/lvm/lvm.conf \
 && grep -q 'global_filter = \[ "a|^/dev/loop\[0-9\]+\$|", "r|\.\*|" \]' /etc/lvm/lvm.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
