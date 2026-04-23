FROM python:3.11-slim

ARG GOWA_VERSION=8.3.3
ARG TARGETARCH=amd64

ENV WHATSBOT_DOCKER=1
ENV PYTHONUNBUFFERED=1
# Default mode: single (backward-compatible). Set to "saas" for multi-tenant.
ENV WHATSBOT_MODE=single
# Optional: set to a volume mount path on Railway to persist data across deploys.
# Example on Railway: set WHATSBOT_DATA_DIR=/data and mount volume at /data
# ENV WHATSBOT_DATA_DIR=

# Install curl and unzip for downloading GOWA
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Download and install GOWA binary for Linux
RUN curl -fsSL "https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/download/v${GOWA_VERSION}/whatsapp_${GOWA_VERSION}_linux_${TARGETARCH}.zip" \
        -o /tmp/gowa.zip && \
    unzip /tmp/gowa.zip -d /tmp/gowa && \
    cp /tmp/gowa/linux-${TARGETARCH} /usr/local/bin/gowa && \
    chmod +x /usr/local/bin/gowa && \
    rm -rf /tmp/gowa /tmp/gowa.zip

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code only
COPY agent/ agent/
COPY config/ config/
COPY gowa/ gowa/
COPY db/ db/
COPY server/ server/
COPY web/ web/
COPY main.py .

# Create bin/gowa symlink so gowa/manager.py finds the binary at expected path
RUN mkdir -p bin && ln -s /usr/local/bin/gowa bin/gowa

# Create runtime directories for single-tenant mode
RUN mkdir -p logs storages statics

# Create runtime directories for SaaS multi-tenant mode
RUN mkdir -p tenants

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]

