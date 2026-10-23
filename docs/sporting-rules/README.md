# Sporting Rules Knowledge Base

> **This content is entirely fictional**, authored for the Pit Wall Copilot
> demo. It is not sourced from, affiliated with, or an accurate description
> of the rules of any real motorsport series. It exists to give Gate 3's
> Knowledge-Base retrieval something real to cite when it explains a
> strategy alert.

Each `.md` file in this folder is one retrievable document. The format is a
small line-based header, parsed by
`backend/app/services/knowledge_base.py` (no YAML dependency required):

```
# <Title>
Doc-Id: <stable id>
Keywords: <comma, separated, keywords>

<body text — this is what gets cited>
```

`KnowledgeBaseService` loads every `.md` file in this folder at startup,
and `retrieve(query_keywords)` does a simple keyword-overlap ranking
against each document's `Keywords:` line and title — this stands in for a
real Bedrock Knowledge Base (which would use managed embeddings + vector
search over a much larger corpus). See the Gate 3 README section for why
this simplification is appropriate at this scale, and what a production
version would need instead.
