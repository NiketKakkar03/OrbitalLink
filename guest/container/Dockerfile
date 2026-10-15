FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git make gcc g++ bash patch tar gzip bzip2 xz-utils perl cpio \
    unzip rsync wget file bc python3 python3-venv openssl libssl-dev libsqlite3-dev \
    build-essential sed findutils diffutils locales && \
    locale-gen en_US.UTF-8 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1001 builder && mkdir /work && chown builder:builder /work
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
USER builder
WORKDIR /work/source
