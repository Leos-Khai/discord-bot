# Multi-stage build to reduce image size and improve security
# Builder stage
FROM python:3.12-alpine AS builder
WORKDIR /app

# Install system dependencies including MongoDB tools
RUN apk add --no-cache mongodb-tools

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Final stage
FROM python:3.12-alpine
WORKDIR /app

# Install MongoDB tools in final stage
RUN apk add --no-cache mongodb-tools

# Copy installed dependencies from builder stage
COPY --from=builder /install /usr/local

# Copy application code, including config.json
COPY . /app

# Expose port and set environment variables
EXPOSE 80
ENV NAME World

# Add healthcheck for MongoDB connection
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "from motor.motor_asyncio import AsyncIOMotorClient; import asyncio, json; \
    with open('/app/src/config.json') as f: config = json.load(f); \
    client = AsyncIOMotorClient(config['mongodb']['uri']); \
    asyncio.run(client.admin.command('ping'))"

# Run the application
CMD ["python", "src/main.py"]
