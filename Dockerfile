FROM python:3.12-slim

# Install system utilities needed for building packages
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements file first to cache pip dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser and all system libraries for Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application source code
COPY app/ ./app/

# Expose default port
EXPOSE 5000

# Set environment variables
ENV FLASK_ENV=production
ENV PORT=5000
ENV PLAYWRIGHT_HEADLESS=true

# Change directory to the app folder to match WSGI expectations
WORKDIR /app/app

# Run the app with Gunicorn and gevent websocket worker
CMD gunicorn wsgi:application \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --workers 1 \
    --bind 0.0.0.0:$PORT \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
