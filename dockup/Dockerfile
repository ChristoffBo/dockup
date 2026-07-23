FROM python:3.14-slim

# Build args for debugging
ARG TARGETARCH
ARG TARGETOS

# Install Docker CLI, Docker Compose, and CIFS utilities for backup feature
#
# NOTE (2026-07-23) — build-essential / python3-dev / libxml2-dev /
# libxslt1-dev / cargo / rustc are here because lxml 5.3.0 had NO Python 3.14
# wheels and compiled from source on every build. lxml 6.1.1 (now pinned in
# requirements.txt) ships cp314 wheels, and bcrypt 5.0.0 ships abi3 wheels, so
# this toolchain is very likely now dead weight — it inflates the image and
# the attack surface for nothing. Do NOT remove it in the same change as the
# dependency bump: get one clean build first, then drop it as its own change
# and confirm nothing falls back to a source build.
RUN apt-get update && \
    apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    wget \
    build-essential \
    python3-dev \
    libxml2-dev \
    libxslt1-dev \
    cargo \
    rustc \
    cifs-utils \
    samba-client && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli docker-compose-plugin && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Trivy - use TARGETARCH (set by buildx), fall back to uname -m
RUN TRIVY_VERSION="0.72.0" && \
    ARCH="${TARGETARCH:-$(uname -m)}" && \
    echo "Installing Trivy ${TRIVY_VERSION} for arch: ${ARCH}" && \
    case "${ARCH}" in \
        arm64|aarch64) TRIVY_ARCH="ARM64" ;; \
        *) TRIVY_ARCH="64bit" ;; \
    esac && \
    wget -qO- "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TRIVY_ARCH}.tar.gz" | tar -xz -C /usr/local/bin trivy && \
    chmod +x /usr/local/bin/trivy

# Install Skopeo for registry operations
RUN apt-get update && \
    apt-get install -y skopeo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Python dependencies — SINGLE SOURCE OF TRUTH is requirements.txt.
# Previously this Dockerfile carried its own inline pip list, which had already
# drifted from requirements.txt (apprise 1.9.6 vs 1.9.8, pytz 2025.2 vs 2026.1,
# psutil 7.2.1 vs 7.2.2). Editing requirements.txt did nothing — the image
# shipped whatever was pinned here. One file now, no drift.
#
# Copied BEFORE the app source on purpose: changing app.py no longer busts the
# pip layer, so rebuilds after a code change skip the whole install.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Create directories including logs, trivy cache, and backups
RUN mkdir -p /app/static /app/data /app/logs /stacks /config/trivy-cache /app/backups /mnt/backup-share

# Copy application files
COPY app.py /app/
COPY backup_manager.py /app/
COPY linuxserver_templates.py /app/
COPY registry_manager.py /app/
COPY registry_deployment.py /app/
COPY static/index.html /app/static/
COPY static/login.html /app/static/
COPY static/setup.html /app/static/

# Set default port (can be overridden with -e PORT=80)
ENV PORT=5000

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/stacks || exit 1

# Run the application - app.py now reads PORT environment variable
CMD ["python", "app.py"]
