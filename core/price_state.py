from collections import deque
import time


class PriceState:
    def __init__(self, max_history_min: int = 15):
        self.max_history_min = max_history_min
        self.last_alert_times = {}
        self.tick_history = deque()

    def add_tick(self, mid: float):
        now = time.time()
        self.tick_history.append((now, mid))
        cutoff = now - self.max_history_min * 60
        while self.tick_history and self.tick_history[0][0] < cutoff:
            self.tick_history.popleft()

    def change_pct_over_window(self, window_min: float) -> float | None:
        if len(self.tick_history) < 2:
            return None

        now = time.time()
        cutoff = now - window_min * 60
        window_ticks = [t for t in self.tick_history if t[0] >= cutoff]
        if len(window_ticks) < 2:
            return None

        oldest = window_ticks[0][1]
        latest = window_ticks[-1][1]
        return (latest - oldest) / oldest * 100 if oldest else None

    def rolling_change_pct(self, window_min: float = 10) -> float | None:
        return self.change_pct_over_window(window_min)

    def can_alert(self, key: str, cooldown_min: float) -> bool:
        last = self.last_alert_times.get(key, 0)
        return (time.time() - last) > cooldown_min * 60

    def mark_alerted(self, key: str):
        self.last_alert_times[key] = time.time()