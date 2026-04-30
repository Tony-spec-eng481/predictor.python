import sys
import socketio
import threading
import requests
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QFrame, QGraphicsDropShadowEffect)
from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont, QPalette

class DataSignals(QObject):
    new_multiplier = pyqtSignal(dict)
    status_update = pyqtSignal(dict)

class BettingOverlay(QMainWindow):
    def __init__(self):
        super().__init__()
        self.signals = DataSignals()
        self.sio = socketio.Client()
        self.is_locked = False
        self.last_pos = None
        self.socket_thread = None  # Initialize socket_thread attribute
        
        self.init_ui()
        self.init_sockets()
        
    def init_ui(self):
        # Window Setup
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(250, 150)
        self.resize(300, 200)
        
        # Main Container
        self.container = QFrame(self)
        self.container.setObjectName("mainContainer")
        self.container.setStyleSheet("""
            #mainContainer {
                background-color: rgba(20, 20, 30, 210);
                border-radius: 15px;
                border: 1px solid rgba(255, 255, 255, 30);
            }
            QLabel { color: white; }
            #multiplierLabel {
                font-size: 32px;
                font-weight: bold;
                color: #00ff88;
            }
            #statusLabel {
                font-size: 12px;
                color: #aaaaaa;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 20);
                border: none;
                border-radius: 5px;
                padding: 5px 10px;
                color: white;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 40);
            }
            #lockBtn {
                background-color: #3d5afe;
            }
        """)
        
        layout = QVBoxLayout(self.container)
        
        # Header (Draggable Area)
        header = QHBoxLayout()
        self.title_label = QLabel("Betting Predictor")
        self.title_label.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        
        self.lock_btn = QPushButton("🔒 Lock")
        self.lock_btn.setObjectName("lockBtn")
        self.lock_btn.setFixedWidth(60)
        self.lock_btn.clicked.connect(self.toggle_lock)
        
        header.addWidget(self.title_label)
        header.addStretch()
        header.addWidget(self.lock_btn)
        
        # Content
        content = QVBoxLayout()
        content.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.multiplier_val = QLabel("1.00x")
        self.multiplier_val.setObjectName("multiplierLabel")
        self.multiplier_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.status_text = QLabel("System Ready")
        self.status_text.setObjectName("statusLabel")
        self.status_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        content.addWidget(self.multiplier_val)
        content.addWidget(self.status_text)
        
        # Footer
        footer = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_collection)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_collection)
        
        footer.addWidget(self.start_btn)
        footer.addWidget(self.stop_btn)

        self.ocr_btn = QPushButton("Set OCR Region")
        self.ocr_btn.clicked.connect(self.set_ocr_region)
        self.ocr_btn.setStyleSheet("background-color: #ff9800;")
        
        layout.addWidget(self.ocr_btn)
        
        layout.addLayout(header)
        layout.addSpacing(10)
        layout.addLayout(content)
        layout.addStretch()
        layout.addLayout(footer)
        
        self.setCentralWidget(self.container)
        
        # Shadow effect
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 5)
        self.container.setGraphicsEffect(shadow)

    def init_sockets(self):
        self.signals.new_multiplier.connect(self.on_multiplier_received)
        self.signals.status_update.connect(self.on_status_received)
        
        @self.sio.on('new_multiplier')
        def handle_new_multiplier(data):
            self.signals.new_multiplier.emit(data)
            
        @self.sio.on('collection_status')
        def handle_status(data):
            self.signals.status_update.emit(data)

        @self.sio.on('status_update')
        def handle_initial_status(data):
            self.signals.status_update.emit({'status': 'running' if data.get('is_running') else 'stopped'})
            if data.get('last_multiplier'):
                self.signals.new_multiplier.emit({'multiplier': data.get('last_multiplier')})
            
        def run_socket():
            try:
                self.sio.connect('http://localhost:5000')
                self.sio.wait()
            except Exception as e:
                print(f"Socket connection failed: {e}")
                
        self.socket_thread = threading.Thread(target=run_socket, daemon=True)
        self.socket_thread.start()

    def on_multiplier_received(self, data):
        mult = data.get('multiplier', 1.00)
        self.multiplier_val.setText(f"{mult:.2f}x")
        
        # Dynamic color based on multiplier
        if mult >= 2.0:
            self.multiplier_val.setStyleSheet("color: #00ff88; font-size: 32px; font-weight: bold;") # Green
        else:
            self.multiplier_val.setStyleSheet("color: #ff4444; font-size: 32px; font-weight: bold;") # Red

    def on_status_received(self, data):
        status = data.get('status', 'unknown')
        self.status_text.setText(f"Status: {status.capitalize()}")

    def toggle_lock(self):
        self.is_locked = not self.is_locked
        if self.is_locked:
            self.lock_btn.setText("🔓 Unlock")
            self.lock_btn.setStyleSheet("background-color: #f44336;")
            # Enable click-through
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowTransparentForInput)
            # Need to call show() to apply new flags
            self.show()
            self.container.setStyleSheet("""
                #mainContainer {
                    background-color: rgba(20, 20, 30, 100);
                    border-radius: 15px;
                    border: 1px solid rgba(255, 255, 255, 30);
                }
                QLabel { color: white; }
                #multiplierLabel {
                    font-size: 32px;
                    font-weight: bold;
                    color: #00ff88;
                }
                #statusLabel {
                    font-size: 12px;
                    color: #aaaaaa;
                }
                QPushButton {
                    background-color: rgba(255, 255, 255, 20);
                    border: none;
                    border-radius: 5px;
                    padding: 5px 10px;
                    color: white;
                }
                QPushButton:hover {
                    background-color: rgba(255, 255, 255, 40);
                }
                #lockBtn {
                    background-color: #3d5afe;
                }
            """)
            self.status_text.setText("Overlay Mode: Click-through")
        else:
            self.lock_btn.setText("🔒 Lock")
            self.lock_btn.setStyleSheet("background-color: #3d5afe;")
            # Disable click-through
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowTransparentForInput)
            # Need to call show() to apply new flags
            self.show()
            self.container.setStyleSheet("""
                #mainContainer {
                    background-color: rgba(20, 20, 30, 210);
                    border-radius: 15px;
                    border: 1px solid rgba(255, 255, 255, 30);
                }
                QLabel { color: white; }
                #multiplierLabel {
                    font-size: 32px;
                    font-weight: bold;
                    color: #00ff88;
                }
                #statusLabel {
                    font-size: 12px;
                    color: #aaaaaa;
                }
                QPushButton {
                    background-color: rgba(255, 255, 255, 20);
                    border: none;
                    border-radius: 5px;
                    padding: 5px 10px;
                    color: white;
                }
                QPushButton:hover {
                    background-color: rgba(255, 255, 255, 40);
                }
                #lockBtn {
                    background-color: #3d5afe;
                }
            """)
            self.status_text.setText("Control Mode: Interactive")

    def start_collection(self):
        try:
            requests.post("http://localhost:5000/api/collection/start", timeout=2)
        except Exception as e:
            print(f"Failed to start collection: {e}")

    def stop_collection(self):
        try:
            requests.post("http://localhost:5000/api/collection/stop", timeout=2)
        except Exception as e:
            print(f"Failed to stop collection: {e}")

    def set_ocr_region(self):
        # Use current window geometry as the OCR region
        geom = self.geometry()
        region = [geom.x(), geom.y(), geom.width(), geom.height()]
        try:
            requests.post("http://localhost:5000/api/ocr/region", json={'region': region}, timeout=2)
            requests.post("http://localhost:5000/api/ocr/start", timeout=2)
            self.status_text.setText(f"OCR Region set: {region}")
        except Exception as e:
            print(f"Failed to set OCR region: {e}")

    # Mouse events for dragging
    def mousePressEvent(self, event):
        if not self.is_locked and event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if not self.is_locked and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None

if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = BettingOverlay()
    overlay.show()
    sys.exit(app.exec())