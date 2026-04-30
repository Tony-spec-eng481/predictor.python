import socketio
import flask_socketio
import gevent
try:
    import geventwebsocket
    print("gevent-websocket installed")
except ImportError:
    print("gevent-websocket NOT installed")

print(f"python-socketio version: {socketio.__version__}")
print(f"flask-socketio version: {flask_socketio.__version__}")
