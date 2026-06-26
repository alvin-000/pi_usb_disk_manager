# buttons.py — GPIO button handler with debounce via gpiozero

import queue
from gpiozero import Button
import config


class ButtonHandler:
    """
    Registers all 7 hardware buttons and emits named events into a Queue.
    The main event loop consumes events from ButtonHandler.queue.
    """

    def __init__(self):
        self.queue: queue.Queue = queue.Queue()

        self._buttons = []

        # Standard buttons — fire on press
        for pin, name in [
            (config.PIN_UP,     config.BTN_UP),
            (config.PIN_DOWN,   config.BTN_DOWN),
            (config.PIN_LEFT,   config.BTN_LEFT),
            (config.PIN_RIGHT,  config.BTN_RIGHT),
            (config.PIN_SELECT, config.BTN_SELECT),
            (config.PIN_B,      config.BTN_B),
        ]:
            btn = Button(pin, pull_up=True, bounce_time=config.DEBOUNCE_MS)
            btn.when_pressed = lambda b=name: self._enqueue(b)
            self._buttons.append(btn)

        # Button A — short press = BTN_A, hold 5s = BTN_A_LONG
        # Use a flag so a long-press does NOT also emit BTN_A on release.
        self._a_held = False

        btn_a = Button(
            config.PIN_A,
            pull_up=True,
            bounce_time=config.DEBOUNCE_MS,
            hold_time=config.A_HOLD_TIME,
        )

        def _on_a_held():
            self._a_held = True
            self._enqueue(config.BTN_A_LONG)

        def _on_a_released():
            if not self._a_held:
                self._enqueue(config.BTN_A)
            self._a_held = False

        btn_a.when_held = _on_a_held
        btn_a.when_released = _on_a_released
        self._buttons.append(btn_a)

    def _enqueue(self, event_name: str):
        # Non-blocking put; discard if queue somehow full (shouldn't happen)
        try:
            self.queue.put_nowait(event_name)
        except queue.Full:
            pass

    def close(self):
        """Release GPIO resources."""
        for btn in self._buttons:
            btn.close()
