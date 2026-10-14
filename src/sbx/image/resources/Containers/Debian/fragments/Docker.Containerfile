USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    iptables \
    procps \
    uidmap \
    fuse-overlayfs \
    slirp4netns \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      docker-ce \
      docker-ce-cli \
      containerd.io \
      docker-buildx-plugin \
      docker-compose-plugin \
      docker-ce-rootless-extras \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/sbx-start-rootless-docker /usr/local/bin/sbx-start-rootless-docker
RUN chmod +x /usr/local/bin/sbx-start-rootless-docker && \
    printf '%s\n' \
      'if [ "$(id -u)" -ne 0 ]; then' \
      '  export XDG_RUNTIME_DIR="/run/user/$(id -u)"' \
      '  export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"' \
      'fi' \
      > /etc/profile.d/sbx-rootless-docker.sh

USER root
