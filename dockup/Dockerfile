FROM python:3.14-slim

# Build args for debugging
ARG TARGETARCH
ARG TARGETOS

# Install Docker CLI and Docker Compose
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
        wget -qO- https://github.com/aquasecurity/trivy/releases/download/v0.58.0/trivy_0.58.0_Linux-ARM64.tar.gz | tar -xz -C /usr/local/bin trivy; \
    else \
        wget -qO- https://github.com/aquasecurity/trivy/releases/download/v0.58.0/trivy_0.58.0_Linux-64bit.tar.gz | tar -xz -C /usr/local/bin trivy; \
    fi && \
    chmod +x /usr/local/bin/trivy

# Set working directory
WORKDIR /app

# Install Python dependencies with latest versions
RUN pip install --no-cache-dir \
    flask==3.1.2 \
    flask-sock==0.7.0 \
    docker==7.1.0 \
    pyyaml==6.0.3 \
    apscheduler==3.11.2 \
    apprise==1.9.6 \
    croniter==6.0.0 \
    requests==2.32.5 \
    pytz==2025.2 \
    psutil==7.2.1 \
    bcrypt==5.0.0 \
    beautifulsoup4==4.12.3 \
    lxml==5.3.0

# Create directories including logs and trivy cache
RUN mkdir -p /app/static /app/data /app/logs /stacks /config/trivy-cache

# Copy application files
COPY app.py /app/
COPY linuxserver_templates.py /app/
COPY static/index.html /app/static/
COPY static/login.html /app/static/
COPY static/setup.html /app/static/

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/stacks || exit 1

# Run the application
CMD ["python", "app.py"]
