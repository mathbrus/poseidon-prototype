"""How tool results read to the model: shell results as plain text with
their tails kept, file reads as numbered lines that say how to read on."""

from agent_host.hands import (
    SHELL_CLIP,
    ExecAction,
    ReadFileAction,
    RunTestsAction,
    _render_read,
    _render_shell,
    shell_text,
)


def test_a_shell_result_reads_as_text_not_json():
    text = _render_shell(
        ExecAction(cmd="ls"),
        {"exit_code": 2, "stdout": "a.py\nb.py\n", "stderr": "ls: c: missing"},
    )
    assert text == "exit code 2\nstdout:\na.py\nb.py\n\nstderr:\nls: c: missing"
    assert not text.startswith("{")


def test_each_stream_keeps_its_tail():
    """A runaway command's output is bounded, and what is kept is how it
    ended."""
    noisy = "x" * (SHELL_CLIP * 3) + "THE END"
    text = shell_text({"exit_code": 1, "stdout": noisy, "stderr": noisy})
    assert len(text) < 2 * SHELL_CLIP + 200
    assert text.count("chars clipped") == 2
    assert text.count("THE END") == 2


def test_a_test_run_renders_like_any_shell_result():
    result = {"exit_code": 1, "stdout": "1 failed", "stderr": ""}
    assert _render_shell(RunTestsAction(), result) == shell_text(result)


def test_a_cut_read_says_how_to_read_on():
    lines = "".join(f"line {n}\n" for n in range(1, 4))
    cut = _render_read(
        ReadFileAction(path="big.py"),
        {"content": lines, "start_line": 1, "total_lines": 5000},
    )
    assert (
        cut.splitlines()[0] == "big.py: lines 1-3 of 5000; pass view_range for the rest"
    )
    assert "     1\tline 1" in cut

    whole = _render_read(
        ReadFileAction(path="small.py"),
        {"content": lines, "start_line": 1, "total_lines": 3},
    )
    assert whole.splitlines()[0] == "small.py: lines 1-3 of 3"

    ranged = _render_read(
        ReadFileAction(path="big.py", view_range=[1, 3]),
        {"content": lines, "start_line": 1, "total_lines": 5000},
    )
    assert ranged.splitlines()[0] == "big.py: lines 1-3 of 5000"
