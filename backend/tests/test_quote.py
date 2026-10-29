from finn.normalise.quote import passes, quote_match, tokens

SOURCE = """
We expect revenue growth of 15 to 17 percent in constant currency for the full year,
and operating margin guidance remains in the band of 20-22%.  The Board has approved
a final dividend of Rs. 22 per share.
"""


def test_exact_quote_is_one():
    q = "We expect revenue growth of 15 to 17 percent in constant currency"
    assert quote_match(q, SOURCE) == 1.0


def test_whitespace_case_and_punctuation_are_ignored():
    q = "WE EXPECT   revenue growth of 15-to-17 percent,\nin constant currency!"
    assert quote_match(q, SOURCE) == 1.0
    assert quote_match("final dividend of Rs 22 per share", SOURCE) == 1.0  # "Rs." vs "Rs"


def test_reworded_quote_is_partial():
    q = "Management anticipates revenue growth of 15 to 17 percent in constant currency terms"
    m = quote_match(q, SOURCE)
    assert 0.4 < m < 1.0


def test_heavily_reworded_quote_falls_below_threshold():
    q = "Top line should rise by about a sixth this year, and margins will stay near twenty one"
    m = quote_match(q, SOURCE)
    assert m < 0.4 and not passes(m)


def test_fabricated_quote_is_zero():
    q = "We will double our headcount and open three new offices in Europe next year"
    assert quote_match(q, SOURCE) == 0.0


def test_one_changed_word_loses_the_runs_that_cross_it():
    q = "We expect revenue growth of 15 to 19 percent in constant currency"  # 17 -> 19
    runs = len(tokens(q)) - 3
    assert quote_match(q, SOURCE) == (runs - 4) / runs  # the 4 runs containing "19" are gone


def test_short_and_empty_quotes():
    assert quote_match("", SOURCE) == 0.0
    assert quote_match("final dividend", SOURCE) == 1.0  # fewer than 4 words: one whole run
    assert quote_match("final bonus", SOURCE) == 0.0
    assert quote_match("anything at all here", "") == 0.0


def test_threshold():
    assert passes(0.4) and not passes(0.39)
