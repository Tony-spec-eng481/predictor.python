#!/bin/bash

# Production start script for the Betting Algorithm Backend

# 1. Ensure we are in the correct directory
cd "$(dirname "$0")"

# 2. Load environment variables
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 3. Default variables if not set
export FLASK_ENV=${FLASK_ENV:-production}
export PORT=${PORT:-5000}

echo "Starting backend in $FLASK_ENV mode on port $PORT..."

# 4. Run with Gunicorn (using gevent worker for SocketIO support)
gunicorn wsgi:application \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --workers 1 \
    --bind 0.0.0.0:$PORT \
    --timeout 120 \
    --log-level info
