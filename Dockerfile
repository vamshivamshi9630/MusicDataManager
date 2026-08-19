FROM python:3.11-slim

# Install git and essential tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure git global identity for cloud worker
RUN git config --global user.name "MusicData Cloud Worker" && \
    git config --global user.email "cloud-worker@musicdata.internal"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY .env.example /app/.env.example

# Set Environment Variables
ENV PORT=8000 \
    HOST=0.0.0.0 \
    CLOUD_MODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
