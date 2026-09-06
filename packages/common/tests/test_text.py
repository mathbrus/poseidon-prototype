from common.text import clip_head, clip_tail, numbered_lines


def test_numbered_lines_is_cat_n():
    assert numbered_lines(["a\n", "b\n"], first=3) == "     3\ta\n     4\tb\n"
    assert numbered_lines([]) == ""


def test_the_two_clips_keep_the_end_that_matters():
    """A document's opening matters; a test run's ending does. Both say
    how much they dropped."""
    text = "a" * 10 + "b" * 10
    assert clip_head(text, 10).startswith("a" * 10) and "10 chars clipped" in clip_head(
        text, 10
    )
    assert clip_tail(text, 10).endswith("b" * 10) and "10 chars clipped" in clip_tail(
        text, 10
    )
    assert clip_head(text, 20) == text and clip_tail(text, 20) == text
