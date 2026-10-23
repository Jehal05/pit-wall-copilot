"""LLM client abstraction — Gate 3.

Two implementations exist behind one interface, mirroring the existing
memory/DynamoDB repository pattern (`app/repositories/factory.py`):

- **TemplateLLMClient** (default; no AWS account needed): this is NOT a
  generative model. It deterministically assembles the narrative directly
  from the grounded facts it is given, using fixed sentence templates.
  Identical inputs always produce byte-identical output, and by
  construction it can never mention a number it wasn't given. Local
  development, CI, and this repository's tests all use this by default.
- **BedrockLLMClient** (opt-in via `LLM_PROVIDER=bedrock`): calls a real
  Anthropic-on-Bedrock model through boto3's `bedrock-runtime`
  `invoke_model`, using the documented Messages-API request/response
  shape. Implemented against that documented API, but — like the
  DynamoDB repository before real AWS credentials were available — **not
  exercised against a live AWS account in this environment.** Treat it as
  implemented-but-unverified until it is run somewhere with real Bedrock
  access and a valid `model_id`.

Whichever client is used, the caller (`explanation_engine.py`)
independently re-checks the returned narrative's numbers against the
grounded facts after generation. This module does not need to be trusted
to enforce "never invent a number" on its own — the grounding guard in
`ExplanationEngine` enforces it regardless of which client produced the
text, which is what makes it safe to point this at a real, non-deterministic
LLM later without weakening the "LLM explains, never invents" architecture
principle.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Sequence

from app.domain.explanation_models import KBCitation


class LLMClientError(RuntimeError):
    """Raised when the underlying LLM call fails."""


class LLMClient(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """A short, stable identifier for which backend produced a
        narrative (e.g. "template", "bedrock") — recorded on
        ExplanationResult for transparency."""
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...

    @abstractmethod
    def explain(
        self,
        *,
        system_prompt: str,
        grounded_facts: dict[str, str],
        citations: Sequence[KBCitation],
    ) -> str:
        """Returns a natural-language narrative. Implementations must use
        only `grounded_facts` and `citations` as their source of truth for
        any number or rule they state — never invent, round beyond what is
        given, or compute a new one. (Enforcement of this contract does
        not rely on the implementation alone — see module docstring.)"""
        ...


class TemplateLLMClient(LLMClient):
    """Deterministic, non-generative stand-in for an LLM. See module
    docstring: this is the default so the demo works with zero AWS
    configuration, and so every test of the grounding contract is
    reproducible rather than dependent on a live model's phrasing."""

    def __init__(self, model_id: str = "template-v1"):
        self._model_id = model_id

    @property
    def provider_name(self) -> str:
        return "template"

    @property
    def model_id(self) -> str:
        return self._model_id

    def explain(
        self,
        *,
        system_prompt: str,  # unused: the template needs no instruction-following
        grounded_facts: dict[str, str],
        citations: Sequence[KBCitation],
    ) -> str:
        del system_prompt

        candidate = grounded_facts.get("candidate", "the candidate")
        target = grounded_facts.get("target", "the target")
        lap = grounded_facts.get("triggering_lap", "the current lap")
        previous_status = grounded_facts.get("previous_status", "CLOSED")
        new_status = grounded_facts.get("new_status", "OPEN")
        window = grounded_facts.get("window", new_status)
        margin_ms = grounded_facts.get("expected_margin_ms", "0")
        trials = grounded_facts.get("trials", "the seeded")
        probability = grounded_facts.get("probability_ahead", "an unknown share")
        p10 = grounded_facts.get("p10_margin_ms")
        p90 = grounded_facts.get("p90_margin_ms")

        sentences = [
            f"On lap {lap}, the undercut watch on {candidate} against {target} moved from "
            f"{previous_status} to {new_status}.",
            f"Pit Wall Copilot's deterministic simulation projects {candidate} finishing the "
            f"comparison window {margin_ms}ms ahead of {target} if {candidate} pits now and "
            f"{target} responds the following lap, keeping the undercut window {window}.",
            f"Across {trials} seeded Monte Carlo trials, {candidate} comes out ahead in "
            f"{probability} of outcomes"
            + (f" (P10={p10}ms, P90={p90}ms)." if p10 is not None and p90 is not None else "."),
        ]
        if citations:
            titles = ", ".join(c.title for c in citations)
            sentences.append(f"Relevant sporting-rules context: {titles}.")

        return " ".join(sentences)


class BedrockLLMClient(LLMClient):
    """Calls an Anthropic model on Amazon Bedrock via boto3's
    `bedrock-runtime` `invoke_model`, using the Messages-API body shape.

    NOT exercised against a real AWS account in this environment (no
    network egress to AWS / no Bedrock access here) — see module
    docstring. `boto3` is already a hard dependency of this project (used
    by the DynamoDB repository), so importing it here adds nothing new.
    """

    def __init__(
        self,
        *,
        model_id: str,
        region_name: str,
        max_tokens: int = 512,
        endpoint_url: str | None = None,
    ):
        import boto3
        from botocore.config import Config

        self._model_id = model_id
        self._max_tokens = max_tokens
        # AWS hackathon-final pass, P2-12: without an explicit Config,
        # boto3's default connect/read timeouts are ~60s each and its
        # default retry behavior (legacy mode, up to several attempts with
        # backoff) can multiply that further. Combined with `explain_alert`
        # now running this call via `run_in_threadpool` (see
        # app/api/routes.py), an unbounded-timeout Bedrock call would still
        # tie up a worker-pool thread for minutes on a hung connection,
        # eventually exhausting the thread pool under concurrent load. A
        # short, explicit bound turns "hangs indefinitely" into "fails
        # fast and predictably," which is what `ExplanationEngine`'s
        # caller needs to be able to surface a timely error to the client
        # instead of a request that never resolves.
        bedrock_config = Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region_name,
            endpoint_url=endpoint_url,
            config=bedrock_config,
        )

    @property
    def provider_name(self) -> str:
        return "bedrock"

    @property
    def model_id(self) -> str:
        return self._model_id

    def explain(
        self,
        *,
        system_prompt: str,
        grounded_facts: dict[str, str],
        citations: Sequence[KBCitation],
    ) -> str:
        user_content = self._render_user_prompt(grounded_facts, citations)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        try:
            response = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
            text = "".join(
                block.get("text", "")
                for block in payload.get("content", [])
                if block.get("type") == "text"
            ).strip()
            if not text:
                raise LLMClientError(f"Bedrock response had no text content: {payload!r}")
            return text
        except LLMClientError:
            raise
        except Exception as exc:  # pragma: no cover - real network/AWS errors, not exercised here
            raise LLMClientError(f"Bedrock invoke_model failed: {exc}") from exc

    @staticmethod
    def _render_user_prompt(grounded_facts: dict[str, str], citations: Sequence[KBCitation]) -> str:
        facts_block = "\n".join(f"- {key}: {value}" for key, value in grounded_facts.items())
        citations_block = (
            "\n\n".join(f"[{c.doc_id}] {c.title}\n{c.excerpt}" for c in citations) or "(none retrieved)"
        )
        return (
            "Grounded facts (use ONLY these numbers; do not compute, round differently, "
            "or estimate any number yourself):\n"
            f"{facts_block}\n\n"
            "Relevant sporting-rules excerpts:\n"
            f"{citations_block}\n\n"
            "Write a short (2-4 sentence) spoken-style explanation of this strategy alert for "
            "a race engineer listening on Alexa+. Refer only to the facts and rules given above."
        )
