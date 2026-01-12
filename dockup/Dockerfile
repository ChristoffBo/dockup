# syntax=docker/dockerfile:1
FROM python:3.14-slim as builder

# Build args for debugging
ARG TARGETARCH
ARG TARGETOS

# Install build dependencies
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
    rustc && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt /tmp/requirements.txt

# Install Python dependencies with BuildKit cache
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cargo/registry \
    pip install --no-cache-dir -r /tmp/requirements.txt

# ============================================
# Final stage - smaller, runtime only
# ============================================
FROM python:3.14-slim

# Build args for debugging
ARG TARGETARCH
ARG TARGETOS

# Install runtime dependencies only (no build tools)
RUN apt-get update && \
    apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    wget && \
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

# Install Trivy - with proper ARM detection
RUN echo "Target architecture: ${TARGETARCH:-unknown}" && \
    if [ "$(uname -m)" = "aarch64" ] || [ "${TARGETARCH}" = "arm64" ]; then \
        wget -qO- https://github.com/aquasecurity/trivy/releases/download/v0.68.1/trivy_0.68.1_Linux-ARM64.tar.gz | tar -xz -C /usr/local/bin trivy; \
    else \
        wget -qO- https://github.com/aquasecurity/trivy/releases/download/v0.68.1/trivy_0.68.1_Linux-64bit.tar.gz | tar -xz -C /usr/local/bin trivy; \
    fi && \
    chmod +x /usr/local/bin/trivy

# Copy Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Set working directory
WORKDIR /app

# Create directories including logs and trivy cache
RUN mkdir -p /app/static /app/data /app/logs /stacks /config/trivy-cache

# Copy application files (these change most frequently, so last)
COPY app.py /app/
COPY linuxserver_templates.py /app/
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
