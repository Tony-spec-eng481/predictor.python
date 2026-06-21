"""
Bet Executor — Playwright-based autonomous bet execution.

Shares the browser context with PlaywrightRoadWorxCollector.
Executes: set_stake → click_bet → monitor_multiplier → click_cashout.
"""

import asyncio
import logging
import time
import random
from datetime import datetime
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class BetExecutor:
    """
    Drives the game UI through Playwright to place and resolve bets.

    Lifecycle per round:
        1. wait_for_bet_phase()   — wait until the game shows "Place Bet" UI
        2. set_stake(amount)      — type the desired amount into the stake input
        3. place_bet()            — click the BET button
        4. monitor_and_cashout()  — watch the live multiplier and click CASHOUT at target
    """

    # -- Known selectors for the Spribe / SplitThePot "Bomber/Crossing" game UI --
    # These may need updating if the game provider changes the DOM.
    SELECTORS = {
        # Bet input field
        "stake_input": [
            'input[type="number"]',
            'input[class*="bet-input"]',
            'input[class*="amount"]',
            '.bet-controls input',
            'input[data-testid="bet-input"]',
        ],
        # Place bet button
        "bet_button": [
            'button.play-button',
            'button:has-text("PLAY")',
            'button[class*="bet-button"]',
            'button[class*="place-bet"]',
            'button:has-text("BET")',
            'button:has-text("Bet")',
            'button:has-text("Place")',
            '.bet-controls button',
        ],
        # Go button (appears while round is live)
        "go_button": [
            'button.advance-button',
            'button:has-text("GO")',
            '.cta button:has-text("GO")',
        ],
        # Cashout button (appears while round is live)
        "cashout_button": [
            'button:has-text("CASHOUT")',
            'button[class*="cashout"]',
            'button[class*="cash-out"]',
            'button:has-text("CASH OUT")',
            'button:has-text("Cash Out")',
        ],
        # Live multiplier display
        "live_multiplier": [
            '[class*="multiplier"]',
            '[class*="coefficient"]',
            '.game-multiplier',
            '[data-testid="multiplier"]',
        ],
        # Game status indicators
        "game_status": [
            '[class*="status"]',
            '[class*="phase"]',
            '.game-status',
        ],
    }

    def __init__(self, page=None, dry_run: bool = True):
        self.page = page
        self.dry_run = dry_run
        self._on_result: Optional[Callable] = None  # Callback for result
        self._on_step_decision: Optional[Callable] = None

    def set_page(self, page):
        """Inject the Playwright page object (shared with collector)."""
        self.page = page

    @property
    def on_result(self):
        return self._on_result

    @on_result.setter
    def on_result(self, callback: Callable):
        self._on_result = callback

    @property
    def on_step_decision(self):
        return self._on_step_decision

    @on_step_decision.setter
    def on_step_decision(self, callback: Callable):
        self._on_step_decision = callback

    # ------------------------------------------------------------------
    # Core execution flow
    # ------------------------------------------------------------------

    async def execute_bet(self, stake: float, target_multiplier: float, step_decider: Optional[Callable[[float], dict]] = None) -> dict:
        """
        Full bet lifecycle: set stake → place bet → monitor → cashout/lose.
        Returns a result dict: { 'won': bool, 'actual_multiplier': float, ... }
        """
        result = {
            "won": False,
            "actual_multiplier": 1.0,
            "stake": stake,
            "target": target_multiplier,
            "payout": 0.0,
            "profit": -stake,
            "error": None,
            "dry_run": self.dry_run,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if not self.page:
            result["error"] = "No browser page available"
            logger.error("BetExecutor: No page set")
            return result

        try:
            if self.dry_run:
                logger.info(
                    f"[DRY RUN] Would bet {stake:.2f} KES targeting {target_multiplier}x"
                )
                # In dry-run, we just observe/simulate the round outcome without acting
                actual = await self._observe_round_outcome(target_multiplier, step_decider)
                result["actual_multiplier"] = actual
                result["won"] = actual >= target_multiplier
                if result["won"]:
                    result["payout"] = stake * target_multiplier
                    result["profit"] = result["payout"] - stake
                else:
                    result["profit"] = -stake
                return result

            # --- LIVE EXECUTION ---
            logger.info(f"Executing LIVE bet: {stake:.2f} KES → target {target_multiplier}x")

            # Step 1: Wait for betting phase
            await self._wait_for_bet_phase()

            # Step 2: Set stake amount
            await self._set_stake(stake)

            # Step 3: Click BET
            await self._click_bet()

            # Step 4: Monitor and cashout
            actual = await self._monitor_and_cashout(target_multiplier, stake, step_decider=step_decider)
            result["actual_multiplier"] = actual
            result["won"] = actual > 0.0

            if result["won"]:
                result["payout"] = stake * actual
                result["profit"] = result["payout"] - stake
                logger.info(f"✅ BET WON: {actual:.2f}x — profit {result['profit']:.2f} KES")
            else:
                result["profit"] = -stake
                logger.info(f"❌ BET LOST: crashed — lost {stake:.2f} KES")

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"BetExecutor error: {e}")

        return result

    # ------------------------------------------------------------------
    # Internal browser automation
    # ------------------------------------------------------------------

    async def _find_element(self, selector_group: str, timeout: int = 5000):
        """
        Check if any selector in the group is visible.
        If timeout is 0, checks immediately without waiting.
        If timeout > 0, polls periodically using short sleeps to avoid choking Playwright.
        """
        selectors = self.SELECTORS.get(selector_group, [])
        if not self.page:
            return None

        # 1. Immediate check first using locator visibility
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                count = await loc.count()
                for i in range(count):
                    elem = loc.nth(i)
                    if await elem.is_visible():
                        logger.debug(f"Found {selector_group} immediately with selector: {sel}")
                        return elem
            except:
                pass

        if timeout <= 0:
            return None

        # 2. Poll periodically with simple sleeps
        start = time.time()
        poll_interval = 0.1 # 100ms
        while (time.time() - start) * 1000 < timeout:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    count = await loc.count()
                    for i in range(count):
                        elem = loc.nth(i)
                        if await elem.is_visible():
                            logger.debug(f"Found {selector_group} after poll with selector: {sel}")
                            return elem
                except:
                    pass
            await asyncio.sleep(poll_interval)

        return None

    async def _wait_for_bet_phase(self, max_wait: int = 60):
        """Wait until the game UI allows placing a bet."""
        logger.info("Waiting for bet phase...")
        start = time.time()
        while time.time() - start < max_wait:
            bet_btn = await self._find_element("bet_button", timeout=0)
            if bet_btn:
                is_disabled = await bet_btn.is_disabled()
                if not is_disabled:
                    logger.info("Bet phase detected — bet button is active")
                    return True
            await asyncio.sleep(0.5)

        raise TimeoutError("Bet phase not detected within timeout")

    async def _set_stake(self, amount: float):
        """Type the stake amount into the input field."""
        inp = await self._find_element("stake_input", timeout=5000)
        if not inp:
            raise RuntimeError("Could not find stake input field")

        # Focus, select all, and type robustly to trigger game UI event handlers
        await inp.focus()
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        await inp.type(str(int(amount)))
        await asyncio.sleep(0.3)
        
        # Verify if value was updated, otherwise fall back to direct fill
        val = await inp.input_value()
        if val != str(int(amount)):
            await inp.fill(str(int(amount)))
            
        logger.info(f"Stake set to {int(amount)} KES")

    async def _click_bet(self):
        """Click the bet/play button."""
        btn = await self._find_element("bet_button", timeout=5000)
        if not btn:
            raise RuntimeError("Could not find bet button")

        is_disabled = await btn.is_disabled()
        if is_disabled:
            raise RuntimeError("Bet button is disabled")

        await btn.click()
        await asyncio.sleep(0.5)
        logger.info("Bet placed")

    async def _monitor_and_cashout(self, target: float, stake: float, timeout: int = 120, step_decider: Optional[Callable[[float], dict]] = None) -> float:
        """
        Watch the live multiplier, click GO to advance, and click CASHOUT when target is reached.
        Returns the actual multiplier at resolution.
        """
        logger.info(f"Monitoring round — will cashout at {target}x (stake: {stake} KES)...")
        
        # Step 0: Wait up to 5 seconds for the round to actually start (GO or CASHOUT button appearing)
        start_wait = time.time()
        round_started = False
        while time.time() - start_wait < 5.0:
            go_btn = await self._find_element("go_button", timeout=0)
            cashout_btn = await self._find_element("cashout_button", timeout=0)
            if go_btn or cashout_btn:
                round_started = True
                break
            await asyncio.sleep(0.1)

        if not round_started:
            logger.warning("Round did not seem to start (neither GO nor CASHOUT button appeared)")
            return 0.0

        start = time.time()
        last_multiplier = 0.0

        while time.time() - start < timeout:
            # 1. Check if the round ended (crashing or winning and resetting to PLAY state)
            play_btn = await self._find_element("bet_button", timeout=0)
            if play_btn:
                is_disabled = await play_btn.is_disabled()
                if not is_disabled:
                    # PLAY button is visible and active again, meaning the round is over!
                    logger.info("PLAY button is active again. Round ended.")
                    return 0.0

            # 2. Get CASHOUT and GO buttons
            cashout_btn = await self._find_element("cashout_button", timeout=0)
            go_btn = await self._find_element("go_button", timeout=0)

            if not cashout_btn and not go_btn:
                # Fuzzy verification: wait 300ms to see if it's a page lag
                await asyncio.sleep(0.3)
                cashout_btn = await self._find_element("cashout_button", timeout=0)
                go_btn = await self._find_element("go_button", timeout=0)
                if not cashout_btn and not go_btn:
                    logger.info("Both GO and CASHOUT buttons disappeared. Round ended.")
                    return last_multiplier

            # 3. Read current multiplier from CASHOUT button text
            current_multiplier = 0.0
            if cashout_btn:
                try:
                    cashout_text = await cashout_btn.inner_text()
                    current_multiplier = self._parse_multiplier_from_cashout(cashout_text, stake)
                except Exception as e:
                    logger.warning(f"Failed to read cashout text: {e}")

            if current_multiplier > 0:
                last_multiplier = current_multiplier

            logger.info(f"Current multiplier: {current_multiplier}x, Last multiplier: {last_multiplier}x, Target: {target}x")

            # 4. Decide action:
            action = None
            reasoning = ""
            confidence = 50.0
            risk_level = "medium"

            if step_decider and current_multiplier > 0:
                step_res = step_decider(current_multiplier)
                action = step_res.get("action")  # "move" or "cashout"
                reasoning = step_res.get("reasoning", "")
                confidence = step_res.get("confidence", 50.0)
                risk_level = step_res.get("risk_level", "medium")
                
                logger.info(f"AI Thinking step at {current_multiplier}x: {reasoning} (Decision: {action.upper()})")
                if self._on_step_decision:
                    self._on_step_decision(current_multiplier, action, reasoning, confidence, risk_level)
            else:
                # Fallback to standard target multiplier check
                if current_multiplier >= target:
                    action = "cashout"
                else:
                    action = "move"

            if action == "cashout" and cashout_btn:
                is_cashout_disabled = await cashout_btn.is_disabled()
                if not is_cashout_disabled:
                    logger.info(f"Cashing out at {current_multiplier}x. Clicking CASHOUT.")
                    try:
                        await cashout_btn.click()
                        # Wait to ensure the cashout registers and round ends
                        await asyncio.sleep(1.5)
                        return current_multiplier
                    except Exception as e:
                        logger.warning(f"Failed to click CASHOUT: {e}")
                else:
                    logger.info("Cashout requested but CASHOUT button is disabled (waiting...)")

            elif action == "move" and go_btn:
                is_go_disabled = await go_btn.is_disabled()
                if not is_go_disabled:
                    logger.info(f"Moving forward from {current_multiplier}x. Clicking GO.")
                    try:
                        await go_btn.click()
                        # Wait for step animation/resolution
                        await asyncio.sleep(1.2)
                    except Exception as e:
                        logger.warning(f"Failed to click GO: {e}")
                else:
                    # GO is disabled. Check if we can cash out what we have
                    if cashout_btn:
                        is_cashout_disabled = await cashout_btn.is_disabled()
                        if not is_cashout_disabled and current_multiplier > 0:
                            logger.info("GO is disabled but CASHOUT is active. Cashing out what we have.")
                            try:
                                await cashout_btn.click()
                                await asyncio.sleep(1.5)
                                return current_multiplier
                            except Exception as e:
                                logger.warning(f"Failed to click CASHOUT: {e}")

            await asyncio.sleep(0.1)

        logger.warning("Monitoring timed out")
        return last_multiplier

    def _parse_multiplier_from_cashout(self, btn_text: str, stake: float) -> float:
        """
        Parse current multiplier from cashout button text.
        Btn text is like: "CASHOUT\n1.10 FUN" or "CASHOUT\n11.00 KES".
        Returns multiplier (e.g. 1.10) or 0.0 if not parsed.
        """
        if not btn_text or stake <= 0:
            return 0.0

        # Remove commas and clean up text
        text_clean = btn_text.replace(",", "").upper().strip()
        # Find any floating point/integer number
        import re
        match = re.search(r"([\d\.]+)", text_clean)
        if match:
            try:
                payout = float(match.group(1))
                return round(payout / stake, 2)
            except:
                pass
        return 0.0

    async def _read_live_multiplier(self) -> Optional[float]:
        """Read the current live multiplier from the game DOM (fallback)."""
        elem = await self._find_element("live_multiplier", timeout=500)
        if not elem:
            return None
        try:
            text = await elem.inner_text()
            import re
            match = re.search(r"(\d+\.?\d*)", text)
            if match:
                return float(match.group(1))
        except:
            pass
        return None

    async def _observe_round_outcome(self, target_multiplier: float, step_decider: Optional[Callable[[float], dict]] = None) -> float:
        """
        [Dry-run only] Simulate a round's outcome with realistic step-by-step delays.
        """
        logger.info(f"[DRY RUN] Simulating round targeting {target_multiplier}x...")
        await asyncio.sleep(1.0) # Wait before starting

        sim_multiplier = 1.0
        multipliers = [1.10, 1.25, 1.44, 1.67, 1.95, 2.30, 2.73, 3.28, 3.98, 4.90, 6.12, 7.80, 10.14, 13.52, 18.59, 26.55, 39.83, 63.74]

        # Simulate each step
        for step_mult in multipliers:
            action = None
            reasoning = ""
            confidence = 50.0
            risk_level = "medium"

            if step_decider and sim_multiplier > 0:
                step_res = step_decider(sim_multiplier)
                action = step_res.get("action")
                reasoning = step_res.get("reasoning", "")
                confidence = step_res.get("confidence", 50.0)
                risk_level = step_res.get("risk_level", "medium")
                
                logger.info(f"[DRY RUN] AI Thinking step at {sim_multiplier}x: {reasoning} (Decision: {action.upper()})")
                if self._on_step_decision:
                    self._on_step_decision(sim_multiplier, action, reasoning, confidence, risk_level)
            else:
                if sim_multiplier >= target_multiplier:
                    action = "cashout"
                else:
                    action = "move"

            if action == "cashout":
                logger.info(f"[DRY RUN] Simulating 'CASHOUT' at {sim_multiplier}x.")
                await asyncio.sleep(1.0)
                return sim_multiplier

            logger.info(f"[DRY RUN] Simulating click 'GO'...")
            await asyncio.sleep(1.2) # Simulate delay

            # Easy difficulty survival probability is ~88%
            if random.random() < 0.88:
                sim_multiplier = step_mult
                logger.info(f"[DRY RUN] Safe! Current multiplier: {sim_multiplier}x")
            else:
                logger.info(f"[DRY RUN] Exploded! Crashed at {sim_multiplier}x")
                return 0.0 # Loss

        return sim_multiplier

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def get_current_balance(self) -> Optional[float]:
        """Try to read the player's balance from the game UI."""
        # Ordered from most-specific to broadest
        selectors = [
            '[data-testid="balance"]',
            '.player-balance',
            '[class*="balance"]',
            '[class*="Balance"]',
            '[class*="wallet"]',
            '[class*="Wallet"]',
            '[class*="funds"]',
            '[class*="credit"]',
            '.controls',
            '.container',
        ]

        import re

        async def try_selector(sel: str) -> Optional[float]:
            try:
                elem = await self.page.wait_for_selector(sel, timeout=3000)
                if elem:
                    text = await elem.inner_text()
                    if text:
                        # Strip commas and look for a number
                        clean = text.replace(',', '')
                        match = re.search(r'(\d+\.?\d*)', clean)
                        if match:
                            val = float(match.group(1))
                            if val >= 1.0:  # sanity check — balance must be >= 1
                                return val
            except:
                pass
            return None

        # Try all selectors concurrently
        results = await asyncio.gather(*(try_selector(sel) for sel in selectors))
        for val in results:
            if val is not None:
                return val

        # Last resort: scan the entire page text for a "number KES" or "number FUN" pattern
        try:
            page_text = await self.page.inner_text('body')
            if page_text:
                clean = page_text.replace(',', '')
                # Look for patterns like "1,234.56 KES" or "1234 FUN"
                matches = re.findall(r'(\d{1,7}\.?\d{0,2})\s*(?:KES|FUN|kes|fun)', clean)
                for m in matches:
                    try:
                        val = float(m)
                        if val >= 1.0:
                            logger.info(f"Balance found via page-text scan: {val}")
                            return val
                    except:
                        pass
        except Exception as e:
            logger.warning(f"Page-text balance scan failed: {e}")

        return None

    async def screenshot(self, name: str = "debug"):
        """Take a screenshot for debugging."""
        if self.page:
            try:
                path = f"/tmp/betexec_{name}_{int(time.time())}.png"
                await self.page.screenshot(path=path)
                logger.info(f"Screenshot saved: {path}")
            except Exception as e:
                logger.warning(f"Screenshot failed: {e}")
