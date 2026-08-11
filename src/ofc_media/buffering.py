from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class BufferDecision:
    target_seconds: int
    startup_seconds: int
    safe_throughput_bps: int
    quality_cap_bps: int
    should_pause: bool
    reason: str

    def to_dict(self) -> dict[str, int | bool | str]:
        return asdict(self)


class DynamicBufferController:
    """Usa somente taxa de pecas verificadas, nunca progresso disperso."""

    def decide(
        self,
        *,
        download_bps: int,
        rendition_bps: int,
        jitter: float,
        buffered_seconds: float,
    ) -> BufferDecision:
        observed = max(0, int(download_bps))
        jitter = min(max(float(jitter), 0.0), 1.0)
        safe = int(observed * (0.78 - 0.28 * jitter))
        ratio = safe / max(int(rendition_bps), 1)
        if ratio >= 2.0:
            target, reason = 24, "banda ampla"
        elif ratio >= 1.3:
            target, reason = 45, "banda estavel"
        elif ratio >= 1.0:
            target, reason = 75, "margem pequena"
        else:
            target, reason = 120, "download abaixo do consumo"
        startup = min(max(12, target // 3), 35)
        cap = max(600_000, int(safe * 0.70))
        return BufferDecision(
            target_seconds=target,
            startup_seconds=startup,
            safe_throughput_bps=safe,
            quality_cap_bps=cap,
            should_pause=ratio < 1.0 and buffered_seconds < startup,
            reason=reason,
        )
