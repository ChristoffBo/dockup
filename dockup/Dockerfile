FROM python:3.11-slim

# Install Docker CLI and Docker Compose
RUN apt-get update && \
    apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    lsb-release && \
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

# Set working directory
WORKDIR /app

# Install Python dependencies
# Force urllib3 1.26.x first to avoid compatibility issues
RUN pip install --no-cache-dir 'urllib3==1.26.18' && \
    pip install --no-cache-dir \
    flask==3.0.0 \
    flask-sock==0.7.0 \
    docker==7.0.0 \
    pyyaml==6.0.1 \
    apscheduler==3.10.4 \
    apprise==1.6.0 \
    croniter==2.0.1 \
    requests==2.31.0 \
    pytz==2024.1

# Create directories
RUN mkdir -p /app/static /app/data /stacks

# Copy application files
COPY app.py /app/
COPY static/index.html /app/static/

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/stacks || exit 1

# Run the application
CMD ["python", "app.py"]
