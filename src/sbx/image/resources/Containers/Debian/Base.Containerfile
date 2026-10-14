ARG BASE_IMAGE=debian:stable-slim
FROM ${BASE_IMAGE} AS sbx-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    git \
    gnupg \
    python3 \
    ripgrep \
    sudo \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Configure the NodeSource Node.js 22.x apt repository directly instead of
# piping https://deb.nodesource.com/setup_22.x into bash.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64|arm64) ;; \
      *) echo "Unsupported architecture for NodeSource Node.js 22.x: $arch" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/share/keyrings /etc/apt/preferences.d /etc/apt/sources.list.d; \
    rm -f \
      /usr/share/keyrings/nodesource.gpg \
      /etc/apt/sources.list.d/nodesource.list \
      /etc/apt/sources.list.d/nodesource.sources; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg; \
    chmod 644 /usr/share/keyrings/nodesource.gpg; \
    { \
      echo "Types: deb"; \
      echo "URIs: https://deb.nodesource.com/node_22.x"; \
      echo "Suites: nodistro"; \
      echo "Components: main"; \
      echo "Architectures: $arch"; \
      echo "Signed-By: /usr/share/keyrings/nodesource.gpg"; \
    } > /etc/apt/sources.list.d/nodesource.sources; \
    { \
      echo "Package: nsolid"; \
      echo "Pin: origin deb.nodesource.com"; \
      echo "Pin-Priority: 600"; \
    } > /etc/apt/preferences.d/nsolid; \
    { \
      echo "Package: nodejs"; \
      echo "Pin: origin deb.nodesource.com"; \
      echo "Pin-Priority: 600"; \
    } > /etc/apt/preferences.d/nodejs

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

USER root
WORKDIR /root
