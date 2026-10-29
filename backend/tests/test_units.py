import pytest

from finn.normalise.units import (
    UnknownUnit,
    convert,
    detect_unit,
    detect_unit_xbrl,
    to_crore,
)


@pytest.mark.parametrize(
    "header, unit",
    [
        ("Rs. in lakhs", "lakh"),
        ("(Rs. in Lakhs)", "lakh"),
        ("₹ in Lacs", "lakh"),
        ("in crores", "crore"),
        ("(in Rs. Cr.)", "crore"),
        ("Rs. Crores", "crore"),
        ("Consolidated Figures in Rs. Crores / View Standalone", "crore"),
        ("Rupees in million", "million"),
        ("INR Mn", "million"),
        ("(Amount in Rs.)", "rupee"),
        ("in INR", "rupee"),
    ],
)
def test_detect_unit(header, unit):
    assert detect_unit(header) == unit


@pytest.mark.parametrize(
    "header",
    ["", "Rs.", "Figures", "in thousands", "Rs. in '000", "USD in billion", "lakhs and crores"],
)
def test_unknown_or_ambiguous_unit_raises(header):
    with pytest.raises(UnknownUnit):
        detect_unit(header)


def test_conversion_to_crore():
    assert to_crore(3_491_500, "lakh") == 34_915.0  # 1 crore = 100 lakh
    assert to_crore(492_440_000_000, "rupee") == 49_244.0  # 1 crore = 10,000,000 rupees
    assert to_crore(1_000, "million") == 100.0  # 1 crore = 10 million
    assert to_crore(49_244.0, "crore") == 49_244.0
    assert convert(635_800, "Rs. in lakhs") == (6_358.0, "lakh")
    with pytest.raises(UnknownUnit):
        to_crore(1.0, "thousand")


def test_xbrl_inr_unit_and_decimals():
    assert detect_unit_xbrl(["iso4217:INR"], "-7") == "rupee"
    assert detect_unit_xbrl(["iso4217:INR"], -5) == "rupee"
    assert detect_unit_xbrl(["iso4217:INR"], "INF") == "rupee"
    for bad in (["iso4217:USD"], ["iso4217:INR", "xbrli:shares"], []):
        with pytest.raises(UnknownUnit):
            detect_unit_xbrl(bad, "-7")
    with pytest.raises(UnknownUnit):
        detect_unit_xbrl(["iso4217:INR"], "seven")
