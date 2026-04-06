"""
Claude API Rate Limiter
=======================
Shared token-bucket rate limiter for parallel Claude API calls.
Used by unt_ocr_correct.py and unt_translate.py.

Anthropic rate limits (as of 2025, Sonnet tier — check your dashboard):
  https://console.anthropic.com/settings/limits

  Default tier:
    Requests per minute (RPM) : 50
    Tokens per minute (TPM)   : 40,000
    Tokens per day (TPD)      : 1,000,000

  Build tier (after spending $5+):
    RPM : 1,000
    TPM : 80,000

This limiter tracks both RPM and TPM. Each call must acquire a slot from
both buckets before proceeding. If either bucket is exhausted, the caller
sleeps until a token is available.

The limiter is conservative by design — it targets 80% of stated limits
to leave headroom for variance in response sizes and processing time.

Usage:
    from claude_rate_limiter import ClaudeRateLimiter

    limiter = ClaudeRateLimiter(rpm=50, tpm=40_000)

    # Before each API call:
    limiter.acquire(estimated_tokens=2000)

    # After each API call (update with actual usage from response headers):
    limiter.record_usage(input_tokens=1500, output_tokens=800)
"""

import time
import threading


class ClaudeRateLimiter:
    """
    Dual token-bucket rate limiter for Anthropic API calls.
    Thread-safe — designed for use with ThreadPoolExecutor.

    Tracks:
      - Requests per minute (RPM)
      - Tokens per minute (TPM) — estimated input + output tokens

    Both buckets refill continuously (not in discrete 60-second windows),
    which is more accurate to how Anthropic actually applies limits.
    """

    def __init__(self, rpm: int = 50, tpm: int = 40_000, safety_factor: float = 0.80):
        """
        Args:
            rpm:           Max requests per minute (check console.anthropic.com/settings/limits)
            tpm:           Max tokens per minute
            safety_factor: Target this fraction of stated limits (default 0.80 = 80%)
                           Reduces the chance of hitting actual limits under variance.
        """
        self._lock = threading.Lock()

        # Apply safety factor
        effective_rpm = rpm * safety_factor
        effective_tpm = tpm * safety_factor

        # Refill rates (per second)
        self._rpm_refill = effective_rpm / 60.0   # requests/sec
        self._tpm_refill = effective_tpm / 60.0   # tokens/sec

        # Bucket capacities (start full)
        self._rpm_capacity = effective_rpm
        self._tpm_capacity = effective_tpm

        # Current bucket levels
        self._rpm_tokens = effective_rpm
        self._tpm_tokens = effective_tpm

        # Last refill timestamp
        self._last_refill = time.monotonic()

        # Stats
        self.total_requests  = 0
        self.total_tokens    = 0
        self.total_waits     = 0
        self.total_wait_secs = 0.0

    def _refill(self):
        """Refill both buckets based on elapsed time. Must be called under lock."""
        now     = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        self._rpm_tokens = min(
            self._rpm_capacity,
            self._rpm_tokens + elapsed * self._rpm_refill
        )
        self._tpm_tokens = min(
            self._tpm_capacity,
            self._tpm_tokens + elapsed * self._tpm_refill
        )

    def acquire(self, estimated_tokens: int = 2000):
        """
        Block until both an RPM slot and TPM tokens are available.
        Call this immediately before each Claude API request.

        Args:
            estimated_tokens: Expected total tokens for this request
                              (input + output). Over-estimate if unsure —
                              it's better to wait slightly longer than to
                              exceed the token budget.
        """
        while True:
            with self._lock:
                self._refill()

                rpm_ok = self._rpm_tokens >= 1.0
                tpm_ok = self._tpm_tokens >= estimated_tokens

                if rpm_ok and tpm_ok:
                    self._rpm_tokens -= 1.0
                    self._tpm_tokens -= estimated_tokens
                    self.total_requests += 1
                    return

                # Calculate how long to wait for each bucket
                wait_for_rpm = 0.0
                wait_for_tpm = 0.0

                if not rpm_ok:
                    # Need (1 - current) more tokens at refill_rate tokens/sec
                    wait_for_rpm = (1.0 - self._rpm_tokens) / self._rpm_refill

                if not tpm_ok:
                    needed = estimated_tokens - self._tpm_tokens
                    wait_for_tpm = needed / self._tpm_refill

                wait = max(wait_for_rpm, wait_for_tpm)
                # Add small jitter to reduce thread synchronization spikes
                wait += 0.05

                self.total_waits     += 1
                self.total_wait_secs += wait

            # Sleep outside the lock so other threads can refill/check
            time.sleep(wait)

    def record_usage(self, input_tokens: int = 0, output_tokens: int = 0):
        """
        Update total token tracking with actual usage from a completed call.
        The response headers or response body from Anthropic contain actual
        usage — call this after each successful API response.

        Note: tokens are already deducted at acquire() time using the estimate.
        This method just tracks statistics — it does NOT further reduce the bucket.
        """
        with self._lock:
            self.total_tokens += input_tokens + output_tokens

    def stats(self) -> dict:
        """Return current rate limiter statistics."""
        with self._lock:
            self._refill()
            return {
                "total_requests":    self.total_requests,
                "total_tokens":      self.total_tokens,
                "total_waits":       self.total_waits,
                "total_wait_secs":   round(self.total_wait_secs, 1),
                "rpm_bucket_level":  round(self._rpm_tokens, 2),
                "tpm_bucket_level":  round(self._tpm_tokens, 0),
            }

    def status_line(self) -> str:
        """Single-line status string for progress displays."""
        s = self.stats()
        return (
            f"RPM bucket: {s['rpm_bucket_level']:.1f}  "
            f"TPM bucket: {s['tpm_bucket_level']:.0f}  "
            f"Waits: {s['total_waits']} ({s['total_wait_secs']}s total)"
        )


# ---------------------------------------------------------------------------
# Tier presets — pass one of these to ClaudeRateLimiter()
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tier presets — check yours at: https://console.anthropic.com/settings/limits
# ---------------------------------------------------------------------------

# Tier 1: Free / low usage
TIER_1 = {"rpm": 50,    "tpm": 40_000}

# Tier 2: Build (after $5 spend)
TIER_2 = {"rpm": 1_000, "tpm": 80_000}

# Tier 3: Scale (after $50+ spend) — per-model limits vary:
#   Haiku:  2,000 RPM, 1,000,000 input TPM, 200,000 output TPM
#   Sonnet: 2,000 RPM, 800,000 input TPM, 160,000 output TPM
#   Opus:   2,000 RPM, 800,000 input TPM, 160,000 output TPM
# We use the Sonnet limits as the conservative default.
TIER_3 = {"rpm": 2_000, "tpm": 800_000}

# Tier 4: Enterprise
TIER_4 = {"rpm": 4_000, "tpm": 2_000_000}

# Legacy aliases
TIER_DEFAULT = TIER_1
TIER_BUILD   = TIER_2
TIER_CUSTOM  = TIER_1  # edit these or use --tier with a number


def limiter_from_tier(tier_name: str) -> "ClaudeRateLimiter":
    """
    Create a rate limiter from a tier preset.

    Args:
        tier_name: "1", "2", "3", "4", or legacy "default", "build", "custom"
    """
    tiers = {
        "1":       TIER_1,
        "2":       TIER_2,
        "3":       TIER_3,
        "4":       TIER_4,
        "default": TIER_1,
        "build":   TIER_2,
        "custom":  TIER_CUSTOM,
    }
    tier = tiers.get(tier_name.lower(), TIER_1)
    return ClaudeRateLimiter(**tier)
