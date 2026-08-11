from ofc_media.buffering import DynamicBufferController


def test_fast_stable_download_uses_small_buffer():
    decision = DynamicBufferController().decide(
        download_bps=20_000_000,
        rendition_bps=3_000_000,
        jitter=0.05,
        buffered_seconds=20,
    )
    assert decision.target_seconds == 24
    assert decision.should_pause is False


def test_slow_download_caps_quality_and_pauses_at_startup():
    decision = DynamicBufferController().decide(
        download_bps=1_000_000,
        rendition_bps=6_000_000,
        jitter=0.5,
        buffered_seconds=2,
    )
    assert decision.target_seconds == 120
    assert decision.quality_cap_bps == 600_000
    assert decision.should_pause is True
