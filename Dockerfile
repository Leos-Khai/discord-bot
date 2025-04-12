# Multi-stage build to reduce image size and improve security
# Builder stage
FROM python:3.12-alpine AS builder
WORKDIR /app
# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
# Final stage
FROM python:3.12-alpine
WORKDIR /app
# Copy installed dependencies from builder stage
COPY --from=builder /install /usr/local
# Copy application code, including config.json
COPY . /app
# Expose port and set environment variables
EXPOSE 80
ENV NAME World
# Run the application
CMD ["python", "src/main.py"]