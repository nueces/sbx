USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/sbx-prepare-kvm /usr/local/bin/sbx-prepare-kvm
RUN chmod 0755 /usr/local/bin/sbx-prepare-kvm
