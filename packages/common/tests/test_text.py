from common.text import numbered_lines


def test_numbered_lines_is_cat_n():
    assert numbered_lines(["a\n", "b\n"], first=3) == "     3\ta\n     4\tb\n"
    assert numbered_lines([]) == ""
