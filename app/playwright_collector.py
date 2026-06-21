import asyncio
import json
import os
import logging
import requests
import re
from datetime import datetime
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

class PlaywrightRoadWorxCollector:
    def __init__(self, api_url="http://localhost:5000/api/multiplier"):
        self.api_url = api_url
        self.is_running = False
        self.browser = None
        self.context = None
        self.page = None
        self.user_data_dir = os.path.join(os.path.dirname(__file__), "playwright_data")
        
        self.login_status = "idle"
        self.collected_count = 0
        self.last_multiplier = None
        self.data_source = "playwright"
        self.ws_hooked = False
        self.errors = []
        self.on_multiplier = None
        
        # Deduplication for final results
        self.recent_finals = {} # { (multiplier, game_id): timestamp }

    async def start_collection(self):
        if self.is_running:
            return
        
        self.loop = asyncio.get_running_loop()
        self.is_running = True
        await self._run_browser()

    async def _run_browser(self):
        async with async_playwright() as p:
            try:
                # Use persistent context to save login state
                self.context = await p.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                
                self.page = await self.context.new_page()
                self.login_status = "logging_in"
                
                self.page.on("websocket", self._handle_websocket)
                
                url = "https://splitthepot.games/sportpesa-ke/bomber/crossing?token=freetoplay&lobby_URL=https:%2F%2Fwww.ke.sportpesa.com%2Fcasino"
                await self.page.goto(url)
                
                logger.info("Playwright browser started. Checking for onboarding modals...")
                # Dismiss onboarding modal if present
                for selector in ['button:has-text("Close")', 'button:has-text("Next")']:
                    try:
                        elem = await self.page.wait_for_selector(selector, timeout=8000)
                        if elem:
                            logger.info(f"Onboarding modal detected. Clicking button: {selector}")
                            await elem.click()
                            await asyncio.sleep(1)
                    except Exception as e:
                        pass

                logger.info("Playwright browser started. Navigate and login as needed.")
                
                while self.is_running:
                    await asyncio.sleep(5)
                    logger.info("Collector heartbeat - scanning for multipliers...")
            except Exception as e:
                logger.error(f"Playwright error: {e}")
                self.errors.append(str(e))
                self.is_running = False
            finally:
                if self.context:
                    await self.context.close()
                self.login_status = "idle"

    def _handle_websocket(self, ws):
        logger.info(f"WebSocket detected: {ws.url}")
        self.ws_hooked = True
        ws.on("framereceived", lambda frame: self._parse_frame(frame))

    def _parse_frame(self, frame):
        try:
            payload = frame
            is_binary = isinstance(frame, (bytes, bytearray))
            
            if is_binary:
                try:
                    payload = frame.decode('utf-8', errors='ignore')
                except:
                    payload = ""
            
            logger.info(f"WS Frame (binary={is_binary}): {payload[:200]}")
            
            # Check for keywords
            keywords = ["crash", "multiplier", "val", "maxMultiplier"]
            found_keyword = any(k in payload.lower() for k in keywords)
            
            if found_keyword:
                if is_binary:
                    # Try binary extraction
                    result = self._extract_binary_multiplier(frame)
                    if result:
                        val, game_id, kw = result
                        # Only 'crash' and 'maxMultiplier' are final round results
                        is_final = kw in [b"crash", b"maxMultiplier"]
                        self._send_to_backend(val, game_id, is_final=is_final)
                        return
                else:
                    # JSON/Text logic
                    try:
                        data = json.loads(payload)
                        self._process_json_data(data)
                        return
                    except:
                        # Regex fallback
                        match = re.search(r'["\'](?:val|multiplier|crash|value)["\']\s*:\s*(\d+\.?\d*)', payload)
                        if match:
                            val = float(match.group(1))
                            kw_found = match.group(0).lower()
                            is_final = "crash" in kw_found or "max" in kw_found
                            
                            id_match = re.search(r'["\'](?:id|round_id|game_id)["\']\s*:\s*["\']([^"\']+)["\']', payload)
                            game_id = id_match.group(1) if id_match else None
                            self._send_to_backend(val, game_id, is_final=is_final)
                            return

        except Exception as e:
            logger.debug(f"Parse error: {e}")

    def _extract_binary_multiplier(self, frame):
        """Scan binary buffer for keywords. Returns (value, game_id, keyword)."""
        import struct
        keywords = [b"crash", b"maxMultiplier", b"multiplier", b"val"]
        
        for kw in keywords:
            idx = frame.find(kw)
            while idx != -1:
                search_limit = min(idx + len(kw) + 15, len(frame) - 8)
                for i in range(idx + len(kw), search_limit):
                    # 0x07 is Double type
                    if frame[i] == 0x07:
                        val_bytes = frame[i+1 : i+9]
                        if len(val_bytes) == 8:
                            try:
                                val = struct.unpack('>d', val_bytes)[0]
                                if 1.0 <= val <= 1000000.0:
                                    # Try to find a round ID nearby
                                    game_id = None
                                    for id_kw in [b"roundId", b"id", b"gameId"]:
                                        id_idx = frame.find(id_kw)
                                        if id_idx != -1:
                                            id_search_limit = min(id_idx + len(id_kw) + 10, len(frame) - 4)
                                            for j in range(id_idx + len(id_kw), id_search_limit):
                                                if frame[j] in [0x04, 0x01]: # Integer types
                                                    id_bytes = frame[j+1 : j+5]
                                                    if len(id_bytes) == 4:
                                                        try:
                                                            rid = struct.unpack('>I', id_bytes)[0]
                                                            game_id = str(rid)
                                                            break
                                                        except: pass
                                            if game_id: break
                                    
                                    return val, game_id, kw
                            except: pass
                idx = frame.find(kw, idx + 1)
        return None

    def _process_json_data(self, data):
        """Specifically look for Road Worx/Bomber patterns in JSON objects."""
        msg_type = data.get('type')
        msg_data = data.get('data')

        if msg_type == 'f_finish' and isinstance(msg_data, dict):
            val = msg_data.get('val') or msg_data.get('multiplier')
            game_id = msg_data.get('id') or msg_data.get('round_id')
            if val is not None:
                self._send_to_backend(float(val), game_id, is_final=True)
                return

        # Generic recursive search
        multiplier = self._find_key_recursive(data, 'multiplier') or \
                     self._find_key_recursive(data, 'val') or \
                     self._find_key_recursive(data, 'crash')
        
        if multiplier and isinstance(multiplier, (int, float, str)):
            try:
                m_val = float(multiplier)
                if m_val >= 1.0:
                    game_id = self._find_key_recursive(data, 'round_id') or \
                             self._find_key_recursive(data, 'id')
                    is_final = (msg_type == 'f_finish') or ('crash' in str(data).lower())
                    self._send_to_backend(m_val, game_id, is_final=is_final)
            except:
                pass

    def _find_key_recursive(self, data, target_key):
        if isinstance(data, dict):
            if target_key in data: return data[target_key]
            for v in data.values():
                res = self._find_key_recursive(v, target_key)
                if res is not None: return res
        elif isinstance(data, list):
            for item in data:
                res = self._find_key_recursive(item, target_key)
                if res is not None: return res
        return None

    def _send_to_backend(self, multiplier, game_round_id=None, is_final=False):
        # 1. Update internal state for UI ticker
        if multiplier != self.last_multiplier:
            self.last_multiplier = multiplier
            if self.on_multiplier:
                self.on_multiplier({"multiplier": multiplier, "is_live": True})

        if not is_final:
            return

        # 2. Collector-side deduplication for Final results
        now = datetime.utcnow().timestamp()
        key = (multiplier, game_round_id)
        
        # Clean up old entries from cache (> 10s)
        self.recent_finals = {k: v for k, v in self.recent_finals.items() if now - v < 10}
        
        if key in self.recent_finals:
            return # Already processed
            
        self.recent_finals[key] = now
            
        try:
            payload = {
                "multiplier": multiplier,
                "game_round_id": game_round_id,
                "source": "playwright_ws",
                "timestamp": datetime.utcnow().isoformat(),
                "is_final": True
            }
            
            self.collected_count += 1
            
            # 3. Post to API for DB persistence
            # Note: app.py will emit the 'new_multiplier' event for history log
            try:
                requests.post(self.api_url, json=payload, timeout=1)
            except Exception as e:
                logger.debug(f"API Post failed: {e}")
                # Fallback: if API fails, we should still notify the UI log
                if self.on_multiplier:
                    self.on_multiplier(payload)
                
            logger.info(f"Captured Final: {multiplier}x" + (f" (ID: {game_round_id})" if game_round_id else ""))
            
        except Exception as e:
            logger.error(f"Failed to process captured data: {e}")

    def stop_collection(self):
        self.is_running = False

    def get_status(self):
        return {
            "is_running": self.is_running,
            "login_status": self.login_status,
            "collected_count": self.collected_count,
            "last_multiplier": self.last_multiplier,
            "data_source": self.data_source,
            "ws_hooked": self.ws_hooked,
            "recent_errors": self.errors[-5:] if self.errors else [],
        }

    def add_round(self, multiplier):
        self.last_multiplier = multiplier
        self.collected_count += 1
        if self.on_multiplier:
            self.on_multiplier(multiplier)
        return self.collected_count
