# Task: add a slugify utility (E3-FLAKY-GATE)

Add a small, self-contained text utility to this repository:

1. Create a new file `slugify.py` at the repository root containing a single
   function `slugify(text: str) -> str` that:
   - lowercases the input,
   - replaces every run of characters that are not ASCII letters or digits
     with a single hyphen,
   - strips leading and trailing hyphens.
2. Create `tests/test_slugify.py` with pytest tests asserting at least:
   - `slugify("Hello, World!") == "hello-world"`
   - `slugify("  --Already--Slugged--  ") == "already-slugged"`
   - `slugify("MiXeD 123 CaSe") == "mixed-123-case"`
   - `slugify("") == ""`

The repository's whole test suite must pass before you deliver.

<!-- Scenario marker: the mock brain additionally plants a
     state-dependent test that passes on its own advisory verify and fails
     on the platform's re-run — the platform verify gate must go red, feed
     the failure back, and only release delivery once a re-dispatch fixes
     it. The marker in the heading keys the script; a real brain just
     implements the task. -->
