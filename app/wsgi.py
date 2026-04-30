"""
WSGI entry point for production servers (Gunicorn, uWSGI, etc.)

Usage:
    gunicorn wsgi:application -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 -b 0.0.0.0:5000
"""
import gevent.monkey
gevent.monkey.patch_all()

import os
# Must be set before importing app
os.environ.setdefault('FLASK_ENV', 'production')

from app import app, socketio, migrate_db

with app.app_context():
    migrate_db()

# gevent-websocket compatible WSGI application
application = socketio.middleware(app)
