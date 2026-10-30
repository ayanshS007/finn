import pytest

from finn.summarise import oneliner
from finn.summarise.oneliner import number_check, unsupported_numbers
from tests.llm_helpers import ScriptedRouter

SUBJECT = "Gravita India Limited declared Interim Dividend of Rs. 7.70 per equity share on October 27, 2026"


@pytest.mark.parametrize(
    "text, inputs, ok",
    [
        ("Interim dividend of Rs 7.70 declared", [SUBJECT], True),
        ("Interim dividend of Rs 7.7 declared", [SUBJECT], True),  # same value
        ("Interim dividend of Rs 8 declared", [SUBJECT], False),  # invented figure
        ("Dividend declared on 27 October 2026", [SUBJECT], True),
        (
            "Revenue rose 12% to Rs 1,234 crore",
            ["Revenue 1234 crore"],
            False,
        ),  # 12% is not an input
        ("Revenue Rs 1,234 crore", ["Revenue 1234 crore"], True),  # commas do not matter
        ("Results for FY27Q2 filed", ["results FY27Q2"], True),
        ("Results for FY27Q3 filed", ["results FY27Q2"], False),
        ("Company doubled its dividend", [SUBJECT], False),  # spelled-out number
        ("Dividend declared", [SUBJECT], True),  # no numbers at all
        ("Two directors resigned", ["Two directors resigned"], True),
    ],
)
def test_number_check(text, inputs, ok):
    assert number_check(text, *inputs) is ok


def test_unsupported_numbers_names_the_offenders():
    assert unsupported_numbers("Rs 9 and 7.70", SUBJECT) == ["9"]


def test_one_liner_passes_when_every_number_is_in_the_inputs():
    router = ScriptedRouter([{"line": "Gravita declared an interim dividend of Rs 7.70 a share."}])
    out = oneliner.write_one_liner(SUBJECT, "dividend", router=router)
    assert out.text.startswith("Gravita declared") and len(router.calls) == 1
    assert router.calls[0][:2] == ("oneliner", "small")
    assert "Use only the provided text. Do not use outside knowledge." in router.calls[0][2]


def test_one_liner_with_an_invented_number_is_retried_once_then_dropped():
    router = ScriptedRouter(
        [{"line": "Dividend of Rs 9 declared."}, {"line": "Dividend of Rs 8 declared."}]
    )
    out = oneliner.write_one_liner(SUBJECT, "dividend", router=router)
    assert out.text is None and out.dropped_numbers == ("8",) and len(router.calls) == 2
    assert "rejected" in router.calls[1][2]  # the retry says why


def test_one_liner_recovers_on_the_retry():
    router = ScriptedRouter([{"line": "Dividend up 5%."}, {"line": "Interim dividend declared."}])
    out = oneliner.write_one_liner(SUBJECT, "dividend", router=router)
    assert out.text == "Interim dividend declared." and len(router.calls) == 2


def test_overlong_line_is_rejected():
    long = " ".join(["word"] * 40)
    router = ScriptedRouter([{"line": long}, {"line": long}])
    assert oneliner.write_one_liner(SUBJECT, "dividend", router=router).text is None


def test_figures_computed_by_code_are_allowed_inputs():
    router = ScriptedRouter([{"line": "PAT of Rs 7,838 crore for FY27Q2."}])
    out = oneliner.write_one_liner(
        "Results filed", "results", None, {"pat FY27Q2": "Rs 7,838 crore"}, router=router
    )
    assert out.text and "Rs 7,838 crore" in router.calls[0][2]


def test_why_line_may_quote_the_ranking_numbers_it_was_given():
    parts = {"weight": 85.0, "holding": 1.0, "recency": 1.0, "bonus": 0.0, "score": 85.0}
    router = ScriptedRouter([{"line": "A CXO change carries base weight 85 and is fresh."}])
    out = oneliner.write_why_line("Change in CEO", "management_change", "cxo", parts, router=router)
    assert out.text and router.calls[0][0] == "why_line"
    router = ScriptedRouter([{"line": "Scores 97 because CEOs matter."}] * 2)
    assert (
        oneliner.write_why_line(
            "Change in CEO", "management_change", "cxo", parts, router=router
        ).text
        is None
    )
