from datetime import date

from app.scheduler import _should_alert


def test_first_ever_availability_alerts():
    assert _should_alert(
        prev_earliest_date=None, prev_exists=False,
        new_earliest=date(2026, 8, 5), alert_before_date=None,
    ) is True


def test_was_empty_now_has_slots_alerts():
    assert _should_alert(
        prev_earliest_date=None, prev_exists=True,
        new_earliest=date(2026, 8, 5), alert_before_date=None,
    ) is True


def test_earlier_date_alerts():
    assert _should_alert(
        prev_earliest_date=date(2026, 9, 1), prev_exists=True,
        new_earliest=date(2026, 8, 5), alert_before_date=None,
    ) is True


def test_same_or_later_date_does_not_alert():
    assert _should_alert(
        prev_earliest_date=date(2026, 8, 5), prev_exists=True,
        new_earliest=date(2026, 8, 5), alert_before_date=None,
    ) is False
    assert _should_alert(
        prev_earliest_date=date(2026, 8, 5), prev_exists=True,
        new_earliest=date(2026, 9, 1), alert_before_date=None,
    ) is False


def test_alert_before_date_suppresses_alert_past_cutoff():
    assert _should_alert(
        prev_earliest_date=None, prev_exists=False,
        new_earliest=date(2026, 9, 5), alert_before_date=date(2026, 9, 1),
    ) is False


def test_alert_before_date_allows_alert_within_cutoff():
    assert _should_alert(
        prev_earliest_date=None, prev_exists=False,
        new_earliest=date(2026, 8, 5), alert_before_date=date(2026, 9, 1),
    ) is True


def test_alert_before_date_suppresses_even_earlier_date_improvement():
    assert _should_alert(
        prev_earliest_date=date(2026, 12, 1), prev_exists=True,
        new_earliest=date(2026, 10, 1), alert_before_date=date(2026, 9, 1),
    ) is False
