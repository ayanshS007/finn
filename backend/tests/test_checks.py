import pytest

from finn.parse.checks import arithmetic_ok, margin_pct, pct_change, qoq, within, yoy


def test_arithmetic_ok_within_one_percent():
    assert arithmetic_ok(11063, 3225, 7838)  # exact
    assert arithmetic_ok(11063, 3225, 7838 * 1.009)  # just inside 1 percent
    assert not arithmetic_ok(11063, 3225, 7838 * 1.02)
    assert not arithmetic_ok(11063, 3225, 9000)


def test_arithmetic_with_exceptional_items_and_other_lines():
    # PBT before exceptional 1000, exceptional loss -100, tax 225, so PAT 675
    assert arithmetic_ok(1000, 225, 675, exceptional=-100)
    assert not arithmetic_ok(1000, 225, 675)  # forgetting the exceptional item fails
    # share of associates adds 37 to profit after tax
    assert arithmetic_ok(8533, 2170, 6400, other=37)


def test_arithmetic_losses_and_zero():
    assert arithmetic_ok(-500, -50, -450)  # loss with a tax credit
    assert arithmetic_ok(0, 0, 0)
    assert not arithmetic_ok(100, 0, 0)


@pytest.mark.parametrize("args", [(None, 1, 1), (1, None, 1), (1, 1, None)])
def test_arithmetic_cannot_confirm_missing_inputs(args):
    assert arithmetic_ok(*args) is False


def test_growth_helpers():
    assert qoq(49244, 48211) == pytest.approx(2.1427, abs=1e-4)
    assert yoy(49244, 44490) == pytest.approx(10.6855, abs=1e-4)
    assert pct_change(110, 100) == pytest.approx(10.0)
    assert pct_change(-50, -100) == pytest.approx(50.0)  # a narrowing loss is an improvement
    assert pct_change(50, 0) is None and pct_change(None, 5) is None and pct_change(5, None) is None


def test_margin_and_within():
    assert margin_pct(11689, 49244) == pytest.approx(23.736, abs=1e-3)
    assert margin_pct(1, 0) is None
    assert within(100.0, 100.9) and not within(100.0, 102.0) and not within(None, 1.0)
