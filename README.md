# DAToolkit

A gated AI diagnostic console. You describe a problem; the AI proposes commands; **you** decide
what runs and what output the AI gets to see.

```
┌──────────────┬──────────────────────────────┐
│  Chat        │  Terminal tabs               │
│  (AI)        │  local · SSH · WinRM         │
├──────────────┴──────────────────────────────┤
│  Command queue: Run · Insert · Edit · Skip  │
└─────────────────────────────────────────────┘
```

- Works with any OpenAI-compatible endpoint: NanoGPT, Ollama, LM Studio, vLLM and others.
- The AI cannot execute anything. Its proposals land in a queue, and only a technician's
  click types a command into a terminal.
- Terminals are real PTYs. `sudo`, Ctrl-C, pagers and device CLIs behave normally.
- Before anything goes back to the AI, you review and edit the exact text. Secrets are
  auto-redacted first.
- API keys and host passwords live in the OS keyring (GNOME Keyring / KWallet), never in
  config files.
- Every case gets an audit log (`events.jsonl`), plain-text terminal transcripts, a
  Markdown export and an AI-written ticket summary.

Licensed under AGPL-3.0-or-later.

## Install (Linux)

```bash
sudo apt install python3-venv python3-gi gir1.2-webkit2-4.1   # Debian/Ubuntu/Mint
git clone <repo> DAToolkit && cd DAToolkit
python3 -m venv --system-site-packages .venv   # system-site-packages gives access to GTK/WebKit
.venv/bin/pip install -e .
.venv/bin/datoolkit
```

`datoolkit --browser` skips the window and prints a local URL to open in a browser instead.
Treat that URL like a password: it grants terminal access.

## First run

1. **Start a case.** Give it a name or ticket number and pick a sensitivity:

   | Sensitivity    | Allowed model tiers        | Use for                              |
   |----------------|----------------------------|--------------------------------------|
   | Open           | Standard, TEE, E2EE, Local | Trials, lab work, non-confidential   |
   | Confidential   | E2EE, Local                | Client data is involved              |
   | Sovereign      | Local only                 | Data must not leave the network      |

2. **Settings → AI providers → Add NanoGPT.** Paste your API key (it goes to the keyring),
   then click **Save & test**. If Claude Opus 5.5 is available it becomes the default model.
   NanoGPT's end-to-end encrypted `private/…` models (e.g. `private/glm-5-3`) are listed too.
3. **+ Session** opens a local shell or a saved host (add hosts under **Settings → Hosts**).
4. Describe the problem in the chat.

## Workflow

- Proposed commands appear in the queue with a risk badge (read only / modifying /
  disruptive). Local rules can raise the AI's own risk label but never lower it. Disruptive
  commands need a second confirmation.
- **Run** types the command and presses Enter. **Insert** types it without Enter, so you can
  finish editing it in the shell. You can edit the command text in the queue before either.
- **Skip** takes an optional note that is passed to the AI (e.g. "not allowed on prod").
- **Send results** collects each command's output from the terminal (from where you ran it
  to where the next one starts), redacts and truncates it, and shows it for review. The AI
  receives only what you send.
- **Send terminal selection** sends any highlighted terminal text.
- Terminal copy/paste: Ctrl+Shift+C / Ctrl+Shift+V.

## Model tiers and end-to-end encryption

| Tier | Detected from | What the provider can see |
|---|---|---|
| **Standard** | anything else | Everything |
| **TEE** | ids starting `TEE/` or `phala/` | The model runs in an enclave, but prompts pass NanoGPT's gateway **in the clear** |
| **E2EE** | NanoGPT `private/…` ids | Ciphertext only, plus your account, the model, timing, sizes and usage |
| **Local** | localhost / private-IP base URL | Nothing leaves your network |

Override any model's tier under **Settings → AI providers → Tier overrides**.

**How E2EE works** (`src/datoolkit/llm/private_mode.py`, following SealedLore's implementation):

1. **Attest.** Before anything is sent (even NanoGPT's billing check), the enclave is attested
   with Tinfoil's own verifier, pinned as `tinfoil==0.14.0`.
   - The hardware report (AMD SEV-SNP or Intel TDX) must match the router release Tinfoil
     published, as recorded by Sigstore.
   - The report must bind the key that requests are sealed to.
   - A release older than the latest on Tinfoil's GitHub is flagged, not refused.
2. **Seal.** The whole request body is sealed on this machine to the attested key (EHBP: HPKE
   X25519 + AES-GCM), including the conversation, the tool definitions and a private
   prompt-cache secret. The reply comes back sealed.
3. **Refuse** anything that doesn't fit:
   - a reply that isn't sealed or isn't marked Private Mode;
   - a stream that ends early;
   - a failed attestation. Nothing is sent, and a `private/` model never falls back to the
     plain endpoint.
4. **Show and log it.**
   - The top bar shows **🔐 attested**; hover for the enclave, release and key fingerprint,
     and click to re-check.
   - Replies are marked "end-to-end encrypted".
   - Each attestation is recorded in the case's `events.jsonl`.
   - Attestation is redone every five minutes.

Like SealedLore, DAToolkit doesn't use NanoGPT's `npx @nanogpt/private-mode@latest` proxy,
because that would let NanoGPT change the code that holds your plaintext.

Verifying an enclave contacts NanoGPT's relay (for the attestation bundle), Sigstore and
GitHub. `TEE/` models aren't attested yet, which is one reason they're excluded from
Confidential cases.

## SSH and WinRM

- **SSH** uses your system `ssh`, so `~/.ssh/config`, keys, the agent, jump hosts and
  known_hosts all work.
  - Password and host-key prompts appear as dialogs. A saved password is supplied once per
    session; if it's rejected, you're asked.
  - Old network gear may need extra options, e.g. `KexAlgorithms=+diffie-hellman-group14-sha1`.
- **WinRM** opens a PowerShell remoting console (pypsrp) supporting NTLM, Kerberos, Negotiate
  and Basic, on 5985 or 5986.
  - Commands are single-line. `Read-Host` and `Get-Credential` prompts and tab completion are
    not supported.
  - Variables persist between commands.

## Where things are stored

| What | Where |
|---|---|
| Config (no secrets) | `~/.config/datoolkit/config.toml` |
| Secrets | OS keyring, service `datoolkit` |
| Case logs, transcripts, exports | `~/.local/share/datoolkit/cases/<case-id>/` |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The core lives in `engine.py` and is frontend-agnostic. The GUI is `server/` plus `web/`,
and a Textual/tmux TUI is planned on the same core.

## Roadmap

- TUI (Textual + tmux, Linux)
- Intel TDX / NVIDIA attestation for `TEE/` models (SealedLore's `tee.py`, `dcap.py`, `nras.py`)
- IP/hostname pseudonymisation with local reverse mapping
- Resume past cases; per-client notes library
- Context compaction for long sessions
