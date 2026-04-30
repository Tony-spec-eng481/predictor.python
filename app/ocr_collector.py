import cv2
import numpy as np
import pytesseract
import pyautogui
import time
import logging
import threading
from datetime import datetime

# Set tesseract path if needed
# pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

class OCRMultiplierCollector:
    def __init__(self, region=None):
        """
        region: tuple (x, y, width, height)
        """
        self.region = region
        self.is_running = False
        self.last_multiplier = None
        self.callback = None
        self.thread = None
        
    def set_region(self, region):
        self.region = region
        logging.info(f"OCR Region set to: {self.region}")

    def capture_and_read(self):
        if not self.region:
            return None
        
        try:
            # Capture screenshot of the region
            screenshot = pyautogui.screenshot(region=self.region)
            # Convert to numpy array for OpenCV
            frame = np.array(screenshot)
            # Convert RGB to BGR (OpenCV format)
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            
            # Preprocessing for better OCR
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Thresholding to get black and white
            _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
            
            # OCR
            text = pytesseract.image_to_string(thresh, config='--psm 7 -c tessedit_char_whitelist=0123456789.x')
            
            # Clean text
            multiplier_text = text.strip().lower().replace('x', '')
            
            try:
                multiplier = float(multiplier_text)
                if multiplier != self.last_multiplier:
                    self.last_multiplier = multiplier
                    return multiplier
            except ValueError:
                pass
                
        except Exception as e:
            logging.error(f"OCR Error: {e}")
            
        return None

    def start_collection(self, callback):
        self.callback = callback
        self.is_running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        logging.info("OCR Collection started")

    def _loop(self):
        while self.is_running:
            multiplier = self.capture_and_read()
            if multiplier and self.callback:
                self.callback(multiplier)
            time.sleep(1) # Check every second

    def stop_collection(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)
        logging.info("OCR Collection stopped")

    def get_status(self):
        return {
            'is_running': self.is_running,
            'region': self.region,
            'last_multiplier': self.last_multiplier
        }
