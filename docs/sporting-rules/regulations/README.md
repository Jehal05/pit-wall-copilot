# Regulation Facts (repair 8 corpus)

> **This content is entirely fictional**, authored for the Pit Wall
> Copilot demo. It is not sourced from, affiliated with, or an accurate
> description of the rules of any real motorsport series. It exists to
> give `get_regulation`'s found/evidence contract real fact-level text to
> match against and cite.

This folder is a **separate, finer-grained** corpus from
`docs/sporting-rules/*.md` (the Gate 3 knowledge base used by
`/explain`'s narrative grounding). That corpus is deliberately
document-level and permissive — a topically-related document is enough to
help an LLM narrate a number it was already given. `get_regulation` (spec
v1.1 repair 8) is answering a direct factual question and must be much
stricter: it should say "I don't know" far more often than it should
guess, including — especially — when a query is *about* a topic this
corpus covers but asks something this corpus does not actually say.

Each file below is one **regulation document**, broken into one or more
**facts** — the smallest unit `get_regulation` can cite. Format:

```
# <Document Title>
Regulation-Id: <stable id, e.g. reg-tires-001>
Version: <e.g. 1.0>

## <Fact title>
Fact-Id: <stable id, unique within this file>
Keywords: <comma-separated keywords that must appear in a query for this
  fact to be considered a candidate match — see regulation_service.py's
  module docstring for the exact matching/threshold algorithm>

<fact text — this is what gets cited verbatim as `excerpt`>

## <next fact>
...
```

`RegulationService` (`app/services/regulation_service.py`) requires a
*high fraction* of a fact's own tagged keywords to appear in the query
before treating that fact as support for it — not just "the query
mentions this general topic." This is deliberate: it is what makes the
service correctly answer `found=false` to a query like "does the tire
rule change after lap 40?", which shares the word "tire" with real facts
below but is not actually supported by any of them.
