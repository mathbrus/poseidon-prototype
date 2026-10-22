# Writing docs

These rules apply to everything a reader sees: the pages under `docs/`,
the root `README.md`, each `packages/*/README.md`, and the docstrings the
site renders into its reference tables. When you change one of those
files, follow these rules and leave it better than you found it.

## Who you are writing for

You are writing for two kinds of reader, and they come in this order:

1. **The evaluator** has just found the project. They want to know what
   Poseidon is, whether it fits their problem, and how much it costs to try.
2. **The person running a company** wants the command, the field or the
   error message, and then goes back to work.

Contributors read the package READMEs and the Contributing section. Those
pages assume the reader has the code open.

## Voice

- **Address the reader as "you".** Write "you approve the merge", not
  "the human approves the merge" or "a person approves the merge".
- **Use the present tense and the active voice.** "The gateway denies the
  call" beats "the call will be denied by the gateway".
- **Use American English:** behavior, organization, honor, defense,
  fulfill, labeled, license. CI rejects the British spellings.
- **Keep each sentence to one idea.** Aim for 25 words or fewer. If a
  sentence needs a dash, a semicolon and a colon, split it.
- **State facts and skip the slogans.** "Spend is a gate, not a warning"
  makes the reader decode a phrase. "When a run reaches its budget, it
  stops and waits for you to approve more" tells them what happens.
- **Don't argue with the reader.** Drop "deliberately", "honest",
  "load-bearing", "worth understanding", "the whole point". If something
  matters, show why it matters in the next sentence.
- **Don't build headings as "X, not Y".** Write "Identity comes from
  verified tokens", not "Identity is a verdict, not a claim".

## Words

- **Define a term the first time a page uses it**, or link it to the
  [glossary](../reference/glossary.md). Terms like steward, crew,
  heartbeat, gate, conductor and bounds mean nothing to a newcomer.
- **Use one name per concept.** The glossary in `CLAUDE.md` fixes the
  names; its "Not" column lists the words to avoid.
- **Name the code only when the reader needs it.** A reader needs
  the command and the field. A file path or a function name belongs in a
  package README.

## Structure

- **Start with what the page is for.** The first paragraph says what the
  reader gets from the page, in two or three sentences.
- **Give every fact one home.** If a fact belongs to another page, link to
  it. Don't restate it. The merge gate, the pause, the tests-per-package
  rule and the package table each live on one page.
- **Put commands in code blocks, never in prose.** Follow each command
  with the output the reader should expect, copied from a real run. Never
  invent output.
- **Use tables for lookups and lists for steps.** Use prose for
  explanation only.
- **Link forward.** End a tutorial or how-to with where to go next.

## Page types

The site follows [Diátaxis](https://diataxis.fr/). Every page is one of
four kinds. Don't mix them.

| Kind | Answers | Shape |
|---|---|---|
| **Tutorial** (Get started) | "Show me it working." | Numbered steps from nothing to a result. Every step has a command and its expected output. No detours into why. |
| **How-to guide** | "How do I…?" | A goal in the title, prerequisites, steps, a way to check it worked, and how to undo it. |
| **Explanation** | "How does this work, and why?" | Prose and diagrams, no commands to run. One idea per page. |
| **Reference** | "What exactly is X?" | Tables generated from the code, with short introductions. No narrative. |

### Tutorial template

```markdown
# <What you will build>

<One paragraph: what you will have at the end, how long it takes, what it costs.>

## Before you start
<Prerequisites as a list.>

## 1. <Step>
<One sentence.>
<command>
<expected output>

## Next steps
<Two or three links.>
```

### How-to template

```markdown
# <Do the thing>

<One sentence: when you need this.>

## Before you start
## Steps
## Check that it worked
## Undo it
```

### Package README template

```markdown
# <package>

<One sentence: what it is. One sentence: who calls it and where it runs.>

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|

## Module map
## How it works          (at most three short subsections; each opens with the rule)
## Invariants            (one line each, with the test that pins it)
## Testing
## Related
```

A package README is 500 to 900 words. Rationale belongs on the
[explanation](../explanation/how-it-works.md) pages, and the README links
there.

## Generated content

Reference tables are rendered from the code by `scripts/gen_docs.py` when
the site builds. A page carries a marker, `<!-- gen: <name> -->`,
where a table belongs. To change the text of a generated table, change
its source: the schema's `description`, the catalog's `summary`, the
command's docstring. Don't edit the page.

Never run `scripts/gen_docs.py` directly. It is a build plugin, and run
by hand it replaces the markers in your working tree with rendered
tables. Build the site instead:

```bash
uv run mkdocs serve        # http://127.0.0.1:8000, rebuilds on save
uv run mkdocs build        # what CI runs; strict, so a broken link fails
```
