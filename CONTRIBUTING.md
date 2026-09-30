# Contributing to DAToolkit

Thanks for helping. DAToolkit is a small project with a clear purpose and a maintainer who
decides what goes in, so please open an issue to discuss anything bigger than a bug fix
before you spend time on it.

## The rule that doesn't bend

**Nothing the AI produces may execute without a technician's action.** The AI proposes;
a person clicks Run. That means no "auto-run" option, no "trust this model" switch, no tool
the model can call that reaches a terminal, a remote desktop, the file system or the network
on its own (web search is approved per query unless the technician opts out for Open cases).
Pull requests that loosen this will be declined, however they are framed.

Two related rules:

- **Case sensitivity is enforced, not advisory.** A Confidential case never reaches a model
  that can read its data in the clear, and a Sovereign case never leaves the network. New
  model paths (helpers, reviewers, summaries) must go through the same tier check.
- **Safety rules may only add caution.** Local risk rules can raise a command's risk but
  never lower what the model said; a reviewer can add warnings but never clear a flag.

## Scope

- **Linux only.** DAToolkit is developed and tested on Debian/Ubuntu/Mint. Ports to Windows
  or macOS are welcome as forks, but the maintainer won't take on a port or its upkeep, so
  please don't send platform abstraction layers upstream.
- **GUI only.** The desktop window (pywebview) and `--browser` mode share one web front end.
  There is no terminal UI and none is planned.
- **Model providers** are anything OpenAI-compatible. Provider-specific code is fine where a
  provider offers something the others don't (NanoGPT's Private Mode, TEE attestation).

## Development

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The core lives in `src/datoolkit/engine.py`; the GUI is `server/` plus `web/`; sessions are in
`sessions/`, model access in `llm/` and the risk, redaction and injection rules in `safety/`.
The training provider (Settings → AI providers → Add training provider) exercises the whole
flow without an API key.

- Match the style of the code around your change. Keep dependencies few; anything that
  handles plaintext or keys is pinned exactly.
- Add tests. Changes to `safety/`, redaction, the tier checks, attestation (`llm/tee.py`,
  `dcap.py`, `nras.py`, `ethsig.py`, `private_mode.py`) or the server's token checks need
  tests that fail without the change.
- After editing risk, sensitive-data, redaction or injection rules, run `datoolkit-replay`
  over your own past cases to see what would now be classified differently.
- Document user-visible behaviour in [MANUAL.md](MANUAL.md).

## Licence

DAToolkit is AGPL-3.0-or-later. By contributing you agree that your contribution is licensed
under the same terms.

## Security issues

Don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
