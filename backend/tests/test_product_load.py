"""A missing/slow measurement must not be turned into a passing load gate."""
from scripts.product_load import assess_limits,timing


def test_empty_and_slow_read_or_ack_measurements_fail_closed():
    assert not any(assess_limits(timing([]),timing([])).values())
    # A tail of21 slow reads is more than5% of400; median remains fast.
    reads=timing([.001]*379+[.501]*21)
    acknowledgements=timing([.001]*4+[1.001])
    assert reads['p50_ms']==1 and reads['p95_ms']==501
    result=assess_limits(reads,acknowledgements)
    assert result=={'read_p95_le_500ms':False,'ack_p95_le_1000ms':False,'ack_max_le_1000ms':False}


def test_reported_limits_are_inclusive_and_ack_max_is_required():
    results=assess_limits(timing([.5]*400),timing([1.0]*5))
    assert all(results.values())
    # With a larger future sample, p95 alone must not hide a slow acknowledgement.
    results=assess_limits(timing([.001]*400),timing([.001]*99+[1.1]))
    assert results['ack_p95_le_1000ms'] is True
    assert results['ack_max_le_1000ms'] is False
