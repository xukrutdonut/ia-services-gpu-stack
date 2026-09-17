FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    curl ca-certificates mesa-vulkan-drivers vulkan-tools libvulkan1 \
    pciutils clinfo procps iproute2 net-tools libatomic1 libgomp1 && \
    rm -rf /var/lib/apt/lists/*