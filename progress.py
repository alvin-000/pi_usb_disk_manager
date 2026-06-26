# progress.py — Progress bar renderer for long-running operations

import time
import config


class ProgressScreen:
    """
    Polls a shared progress dict and renders it on the OLED.

    progress dict schema:
        label   : str   — description shown above the bar
        percent : float — 0.0 to 100.0
        done    : bool  — set True when operation finishes
        error   : str   — non-empty string on failure (optional)

    Usage:
        progress = {"label": "Working...", "percent": 0.0, "done": False, "error": ""}
        disk_ops.some_op(..., progress)
        screen = ProgressScreen(display, button_queue, progress)
        screen.run()   # blocks until done, then shows result and waits for button
    """

    REFRESH_INTERVAL = 0.5  # seconds

    def __init__(self, display, button_queue, progress: dict):
        self.display = display
        self.queue = button_queue
        self.progress = progress

    def run(self):
        """
        Block until the operation completes, then show result.
        Raises config.ReturnToMainMenu if A is held for 5 s during the operation.
        """
        while not self.progress.get("done", False):
            self._render()
            # Drain queued events; raise on long-press A
            try:
                event = self.queue.get_nowait()
                if event == config.BTN_A_LONG:
                    raise config.ReturnToMainMenu()
            except config.ReturnToMainMenu:
                raise
            except Exception:
                pass
            time.sleep(self.REFRESH_INTERVAL)

        # Final render
        self._render()
        time.sleep(0.3)

        # Show result
        error = self.progress.get("error", "")
        if error:
            self._show_result(success=False, message=error)
        else:
            self._show_result(success=True, message="Done!")

        # Wait for any button press to dismiss
        self._wait_for_button()

    def _render(self):
        label = self.progress.get("label", "Working...")
        percent = self.progress.get("percent", 0.0)
        speed = self.progress.get("speed", "")
        self.display.draw_progress(label, percent, speed)

    def _show_result(self, success: bool, message: str):
        if success:
            lines = ["", "  Complete!", "", message[:22], "", "Press any button"]
        else:
            # Wrap long error messages at 22 chars
            words = message.split()
            lines_out = []
            current = ""
            for w in words:
                if len(current) + len(w) + 1 <= 22:
                    current = (current + " " + w).strip()
                else:
                    lines_out.append(current)
                    current = w
            if current:
                lines_out.append(current)
            lines = ["  ERROR:"] + lines_out[:3] + ["Press any button"]

        self.display.draw_message(lines, highlight_last=True)

    def _wait_for_button(self):
        # Clear stale events first
        while True:
            try:
                self.queue.get_nowait()
            except Exception:
                break
        # Block indefinitely until a button is pressed
        self.queue.get()
        raise config.ReturnToMainMenu(silent=True)
