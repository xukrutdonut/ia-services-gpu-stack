# Open Terminal (slim) + pip + full toolchain for programming and agentic AI.
#
# The upstream slim image ships a source-built Python 3.12 with a curated set of
# packages but NO pip on purpose ("no runtime apt/pip installation" by design).
# This layer adds pip, a system toolchain and the Python/Node packages an agent
# needs, without touching the app itself. Base ENTRYPOINT/CMD are inherited.
FROM ghcr.io/open-webui/open-terminal:slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_PREFIX=/opt/npm-global \
    PATH=/opt/npm-global/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---- System toolchain -------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build pkg-config \
        libffi-dev libssl-dev libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev libpng-dev \
        git git-lfs curl wget ca-certificates gnupg unzip zip xz-utils bzip2 \
        jq ripgrep fd-find bat tree less nano vim-tiny \
        procps psmisc htop tmux \
        openssh-client rsync \
        sqlite3 libsqlite3-dev postgresql-client redis-tools \
        file lsof strace netcat-openbsd dnsutils iputils-ping socat \
        shellcheck fonts-dejavu-core \
        nodejs npm \
    && ldconfig \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && ln -sf /usr/bin/batcat /usr/local/bin/bat \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# GitHub CLI (Debian trixie ships it; never fail the build over it)
RUN (apt-get update && apt-get install -y --no-install-recommends gh) \
    || echo "gh not available in apt — skipped"; \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- pip itself + Python packages ------------------------------------------
COPY requirements.txt /opt/requirements.txt
RUN python3 -m ensurepip --upgrade \
    && python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python3 -m pip freeze --exclude pip --exclude setuptools --exclude wheel \
         > /opt/base-constraints.txt \
    && python3 -m pip install --no-cache-dir \
         -c /opt/base-constraints.txt -r /opt/requirements.txt \
    && python3 -m pip check

# ---- Node/TS CLIs -----------------------------------------------------------
RUN mkdir -p /opt/npm-global \
    && npm install -g --no-fund --no-audit \
         typescript tsx prettier pnpm \
         @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force || true

# App user (uid 1000) must be able to npm/pip install at runtime without root:
# npm goes to /opt/npm-global (outside the /home/user volume), pip falls back to
# ~/.local automatically because site-packages stays root-owned on purpose.
RUN chown -R user:user /opt/npm-global \
    && mkdir -p /home/user/.local/bin && chown -R user:user /home/user

# Extras añadidos en su propia capa: tocar requirements.txt invalida la capa
# grande de pip (--no-cache-dir, ~15 min), así que lo que se añada después vive
# aquí.
RUN python3 -m pip install --no-cache-dir GitPython \
    && python3 -m pip check

USER root
