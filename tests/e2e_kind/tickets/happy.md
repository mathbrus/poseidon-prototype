# Task: add a slugify utility

Add a small, self-contained text utility to this repository:

1. Create a new file `slugify.py` at the repository root containing a single
   function `slugify(text: str) -> str` that:
   - lowercases the input,
   - replaces every run of characters that are not ASCII letters or digits
     with a single hyphen,
   - strips leading and trailing hyphens.
2. Create `tests/unit/test_slugify.py` with pytest tests asserting at least:
   - `slugify("Hello, World!") == "hello-world"`
   - `slugify("  --Already--Slugged--  ") == "already-slugged"`
   - `slugify("MiXeD 123 CaSe") == "mixed-123-case"`
   - `slugify("") == ""`

Keep the diff to exactly these two files. The repository's whole test suite
must pass before you deliver.
