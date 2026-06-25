FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    flex \
    bison \
    bc \
    libssl-dev \
    libelf-dev \
    dwarves \
    ca-certificates \
    curl \
    xz-utils \
    tar \
    coreutils \
    binutils \
    procps \
    apparmor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
