# Models and the agent skill

## Models

Tome is provider-agnostic: any OpenAI-compatible endpoint works. The configuration
it is built and tested against is **Gemma 4** (`gemma4:31b`), served through the
Ollama Cloud API (`https://ollama.com/v1`), for both the text path (translation,
memory extraction, the librarian) and the vision path (one-pass page OCR plus
translation). Vision and text point at the same multimodal checkpoint by default;
`OPENAI_VISION_MODEL` can split them.

Gemma 4 ships under **Apache 2.0**: open weights with a licence that lets anyone
use, modify and redistribute them, including commercially. Running it yourself
needs only an OpenAI-compatible runtime and `OPENAI_BASE_URL` pointed at it;
Ollama Cloud is the hosted equivalent.

- Model card: <https://ai.google.dev/gemma/docs/core/model_card_4>
- Licence: <https://ai.google.dev/gemma/apache_2>
- Announcement: <https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/>
- Technical report: <https://arxiv.org/abs/2607.02770>

`services/llm.py` talks to anything that speaks Chat Completions, and no vendor
SDK is imported anywhere, so changing the model is an environment change, not a
code change.

## Agent skill

The librarian ships as a portable **Agent Skill** at
[`skills/tome-librarian/SKILL.md`](../skills/tome-librarian/SKILL.md), written to the
[Agent Skills specification](https://agentskills.io/specification): a `SKILL.md`
with `name` and `description` frontmatter plus a reference file listing the HTTP
surface. Any skills-capable client (Claude Code, Hermes, OpenCode, Copilot, …)
loads the metadata at startup and the instructions only when a task matches, so a
fresh agent can drive a Tome instance without reading the source. It is the same
tool surface the in-app librarian uses.
