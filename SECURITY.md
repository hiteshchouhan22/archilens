# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue:
**[open a private report](https://github.com/saurabh-oss/archilens/security/advisories/new)**
(GitHub private vulnerability reporting). Include the version (`archilens --version` or the action
version), what an attacker could do, and the steps to reproduce.

You'll get an acknowledgement within a few days. Fixes ship in a patch release, and the advisory is
published once a fixed version is out. Reporters are credited unless they'd rather not be.

## Supported versions

Only the latest minor release (currently 0.15.x) gets security fixes. ArchiLens is 0.x, so upgrading
is the fix.

## What ArchiLens does with your code and keys

These are the places worth scrutinising, and what ArchiLens does at each.

**Static analysis is local.** `analyze`, `check`, `diff` and `history` read your repository and Git
objects on the machine they run on. Nothing is sent anywhere unless AI is turned on.

**AI is opt-in, and only sends what is documented.** Without an API key, or with `--no-ai`, no model is
called. With a key, ArchiLens sends module outlines (signatures, no bodies), the source of detected
entry-point files, and, for `diff --ai`, the change table, the list of changed file paths and outlines
of new modules. The README lists exactly what each feature sends, under "What is sent to the model".
Answers are cached in `~/.archilens/ai_cache.db`; the MCP server caches snapshots in
`~/.archilens/snapshots.db`.

**The GitHub Action and your API key.** The action is off for AI by default (`enable-ai: "false"`).
When you turn it on, your key is a secret in your own repository and is passed, as an environment
variable, only to the two steps that call the model; it is billed to your account. GitHub does not
pass secrets to workflows triggered by pull requests from forks, so an outside contributor's PR cannot
spend your key; it gets the static report.

**Model output posted to pull requests is treated as untrusted.** Code in a pull request can try to
steer the model. Before the review is posted, HTML is escaped, images are neutralised, `@mentions`
are broken so nobody is notified, and the text cannot change the action's violation count. The rule
checks and `fail-on-violations` never depend on model output.

**Local servers bind to localhost.** `archilens serve` (the viewer) and `archilens serve-mcp
--transport sse` listen on `127.0.0.1` by default. Binding them to another interface exposes your
repository's architecture, and the viewer's re-analyse endpoint, to that network.
