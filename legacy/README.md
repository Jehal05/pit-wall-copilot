# `legacy/` — optional, non-primary components

Spec v1.1 repair 10: "Retain the legacy Alexa Skill only as an explicitly
optional fallback under a clearly named legacy/experimental directory, or
remove it if it creates confusion."

Everything under this directory is kept for reference only. **Nothing
here is required to satisfy the spec, and nothing here is the primary
integration for anything it overlaps with.**

## `alexa_skill_legacy_rest/`

The original (pre-repair-10) Alexa Skill: an `ask-sdk-core` AWS Lambda
handler that calls the backend's REST API **directly**, duplicating
orchestration logic the MCP tools in `../mcp_server/server.py` already
provide.

**The primary Alexa+ integration is `../alexa_plus_client/`** (a real MCP
client against the real Streamable HTTP `mcp_server/` transport — see
that directory's own README). Do not deploy or present this legacy
package as the required Alexa+ MCP integration; see
`alexa_skill_legacy_rest/lambda/lambda_function.py`'s own module
docstring for the full explanation, and `../README.md` §7b for setup
instructions if you specifically want to run this legacy package's own
tests.
