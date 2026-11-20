import pytest
from booking_agent.display import explicit_dates, human_date, human_time
from booking_agent.single_instance import SingleInstance


@pytest.mark.parametrize('text', ['19 November 2026', '19th Nov 2026', 'November 19, 2026', '2026-11-19'])
def test_natural_dates(text):
    assert explicit_dates(text) == {'2026-11-19'}


def test_date_conflicts_not_hidden():
    assert len(explicit_dates('19 November 2026 or 20 November 2026')) == 2
    assert explicit_dates('31 February 2026') == set()


def test_display_is_locale_independent():
    assert human_date('2026-11-19') == '19 November 2026'
    assert human_time('17:00') == '5 pm'
    assert human_time('00:15') == '12:15 am'


def test_duplicate_panel_is_rejected_and_lock_released(tmp_path):
    first = SingleInstance('dummy-test-token')
    second = SingleInstance('dummy-test-token')
    first.path = second.path = tmp_path/'panel.lock'
    with first:
        with pytest.raises(RuntimeError, match='уже запущена'):
            with second:
                pass
    with SingleInstanceTest(first.path):
        pass


class SingleInstanceTest(SingleInstance):
    def __init__(self, path):
        super().__init__('dummy-test-token')
        self.path = path
