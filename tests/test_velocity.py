from app.normalizers.base import NormalizedEventSchema
from app.incidents import scoring


def _event(endpoint="/profile", http_status=200):
    # Deliberately a low-impact event so velocity is the dominant factor under test.
    return NormalizedEventSchema(
        source="product", service="svc", environment="production",
        endpoint=endpoint, url="https://api/x", http_status=http_status,
        exception_type=None, message="m", user_id=None, country=None,
        platform=None, release=None, fingerprint="fp", raw_payload={},
    )


def test_velocity_tiers_on_windowed_count():
    # window = 5 min, so events_in_window / 5 = errors per minute.
    assert scoring.score_error_velocity(100) == 60   # 20/min
    assert scoring.score_error_velocity(50) == 40    # 10/min
    assert scoring.score_error_velocity(25) == 20    # 5/min
    assert scoring.score_error_velocity(10) == 10    # 2/min
    assert scoring.score_error_velocity(4) == 0      # <2/min


def test_burst_scores_higher_than_trickle_for_same_total():
    # Same cumulative event_count (100), but the sliding window distinguishes them.
    burst = scoring.calculate_criticality(_event(), event_count=100, events_in_window=100)
    trickle = scoring.calculate_criticality(_event(), event_count=100, events_in_window=3)
    assert burst.breakdown["error_velocity"] == 60
    assert trickle.breakdown["error_velocity"] == 0
    assert burst.total_score > trickle.total_score


def test_falls_back_to_event_count_when_no_window():
    # Backward-compatible: no windowed count provided -> uses event_count.
    r = scoring.calculate_criticality(_event(), event_count=100, events_in_window=None)
    assert r.breakdown["error_velocity"] == 60
