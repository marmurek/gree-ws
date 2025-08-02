FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    patchutils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY patches/ patches/
COPY apply_patches.sh .
COPY entrypoint.sh .

# Apply patches
RUN chmod +x apply_patches.sh && \
    chmod +x entrypoint.sh && \
    ./apply_patches.sh

COPY main.py .

# Expose port
EXPOSE 8123

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8123/ || exit 1

# Run the application
ENTRYPOINT ["./entrypoint.sh"]