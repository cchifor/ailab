# Image for ../test-fstrim-converge-docker.sh: Ubuntu 24.04 (the dev workers' base) with systemd as PID 1,
# util-linux's stock fstrim.timer, e2fsprogs, and the ansible bundle the repo's converge tests pin (14.0.0).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends ca-certificates e2fsprogs util-linux procps \
      python3 python3-venv systemd systemd-sysv dbus \
 && python3 -m venv /opt/ansible && /opt/ansible/bin/pip install -q ansible==14.0.0
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
