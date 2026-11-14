"""Observed delivery timing based on native token counters, never SSE chunk counts."""
import math


class NativeCounterWindow:
    def __init__(self):
        self.first_count = self.last_count = None
        self.first_at = self.last_at = None
        self.invalid = False

    def observe(self, count, now):
        if self.invalid or count is None:
            return
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                or not math.isfinite(now)):
            self.invalid = True
            return
        if self.last_count is not None and (count < self.last_count or now < self.last_at):
            self.invalid = True
            return
        if count == 0:
            return
        if self.first_count is None:
            self.first_count = self.last_count = count
            self.first_at = self.last_at = now
        elif count > self.last_count:
            self.last_count, self.last_at = count, now
        # Final/usage events often repeat a count after generation has ended.
        # Their arrival must not stretch the measured decode window.

    def metrics(self):
        if self.invalid or self.first_count is None:
            return {}
        tokens = self.last_count - self.first_count
        seconds = self.last_at - self.first_at
        if tokens <= 0 or seconds <= 0:
            return {}
        return {"observed_decode_tokens": tokens,
                "observed_decode_seconds": seconds,
                "observed_decode_tokens_per_second": tokens / seconds}
