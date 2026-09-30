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
- If the terminal buffer no longer has a command's output (page reloaded, session closed,
  case resumed), the output is taken from the session's transcript file instead. The
  review dialog says when this happened.
- The review dialog also warns when captured output looks like a prompt injection (text
  that tries to give the AI instructions). The AI is told to ignore such text, but you see
  it first and can edit it out.
- **Resume a case**: the case dialog lists earlier cases. Opening one restores the
  conversation, chat and queue; open sessions carry over.
- The top bar shows `ctx 23k`: the prompt size of the last request. It turns amber past the
  threshold in **Settings → General** (default 100k), which is the cue to export a ticket
  summary and start a fresh case.
- Terminal copy/paste: Ctrl+Shift+C / Ctrl+Shift+V.

### Hypothesis board

The AI keeps an explicit list of hypotheses with a confidence each, updated after every
result, shown above the chat. Pin one (📌) to tell the AI to focus on it, or rule it out (✕);
your marks are shown to the AI and survive its updates. The board goes into the ticket
summary and the Markdown export.

### Recipes

**Recipes ▾ → Recipe library** lists pre-written procedures for the active session's OS:
where the disk went (the WinDirStat question), who is on the LAN (the Advanced IP Scanner
question, run it in a session on the far side of a VPN to see that side), path and latency,
disk health, DNS and reachability, baseline snapshots and watch helpers. Each step lands in
the queue as a normal item; "Queue with install" adds the tool's install command as a
separate modifying item first. The AI can queue a recipe too (`run_recipe`). Your own
recipes go in `~/.config/datoolkit/recipes/*.toml`:

```toml
[[recipe]]
id = "printer-linux"
name = "Printer queue"
os = "linux"          # linux | windows | any
tags = ["print"]
steps = [
  { command = "lpstat -p -d", purpose = "Printers and default", risk = "read_only" },
]
```

### Baselines

Queue the **Baseline snapshot** recipe on a healthy host, run the items, then **Recipes ▾ →
Save baseline**. Later, run the same recipe and **Diff against saved baseline**: a unified
diff per section (services, ports, routes, disks, packages, firewall, cron, users, …) that
you can send to the AI. Baselines live under `~/.local/share/datoolkit/baselines/<host>/`.

### Safer changes

- **Blast radius.** A command that would cut the session it runs in (restarting sshd or the
  network on the SSH host, disabling the adapter that carries WinRM, a firewall default-drop,
  `reload` on a device, …) is marked in red and the confirmation says so.
- **Rollback ledger.** The AI must give a rollback for every modifying or disruptive command;
  missing ones are shown in amber. **Recipes ▾ → Rollback ledger** lists the changes that
  ran, newest first, and queues their rollbacks for you to run one by one.
- **Dry run.** For commands with a rehearsal form (`rsync -n`, `apt -s`, `-WhatIf`,
  `terraform plan`, `kubectl --dry-run`, `ansible --check`, `ls` before `rm`, a firewall
  backup before a firewall change), the **Dry run** button queues the rehearsal before the
  real item.
- **Second opinion.** **2nd opinion** on any state-changing item, or from the disruptive
  confirmation, asks a reviewer model what could go wrong and ends with a verdict. Set a
  different model under **Settings → General** (it must be allowed by the case sensitivity);
  the reviewer sees only the command and the case notes, never the proposer's reasoning.
- **Paired probes.** When the AI gives two commands the same `group` (a capture on one side,
  a ping from the other), **Run group** types them into their sessions at the same moment and
  the results carry start times.
- **Watch.** **Watch** on a read-only item wraps it in a bounded loop (every N seconds, M
  samples). When you send the result, iterations identical to the previous one are dropped.

### Photos, replay, context

- **📷** (or paste an image) attaches a photo of a screen, LED panel or label to the next
  message. Photos are resized locally, stored in the case directory and sent to the model
  unredacted, under the same sensitivity gate as text.
- **Export ▾ → Timeline replay** plays the case back: events on the left, the terminal
  transcript on the right, on one slider.
- **Export ▾ → What the AI knows** shows the exact context the model gets next turn, with a
  size estimate per exchange, and lets you remove exchanges from it. The chat and the audit
  log keep them.
- **Export ▾ → Client update** writes the plain-language version of the ticket note.
- **Export ▾ → Distil runbook** turns a solved case into `runbook.md`. When a new case's
  first message resembles a past case, the similar cases are noted in the chat and their
  runbooks go into the AI's context as leads. **Similar past cases…** searches by hand.

### Companion view

`datoolkit --companion` also serves a phone-sized, read-mostly page on the LAN and prints
its URL: the AI's last message (optionally read aloud), the hypothesis board and the queue,
with "I ran it" and "Skip". Its token cannot reach a terminal, open sessions or change
settings. The main URL is reachable on the LAN too while this flag is on, so keep it private.

### Tool cache

**Settings → Tool cache** holds vetted portable CLI tools (WizTree, nmap, Sysinternals, …)
with a SHA-256 each. **Send to session** queues the transfer: `scp` from a local shell for
SSH hosts, or an inline PowerShell write (under 4 MB) for WinRM sessions, followed by the
tool's run command. The hash is checked on arrival and nothing runs without your click.

### Training provider

**Settings → AI providers → Add training provider** adds a scripted fake model
(`training://disk-full`, `training://vpn-one-way`). It walks through a canned case one turn
per message, proposes commands and updates the hypothesis board, so a new technician can
learn Run, Skip and Send with no API cost and nothing leaving the machine. It counts as a
Local-tier model. Scenarios are TOML files under `src/datoolkit/training/scenarios/`.

### Rules replay

`datoolkit-replay` runs today's risk, blast-radius, redaction and injection rules over past
cases' event logs and prints what would now be classified differently. Use it after editing
the rules.

### Keyboard shortcuts

| Keys | Action |
|---|---|
| Alt+1 … Alt+9 | Switch terminal tab |
| Ctrl+Shift+Enter | Run the next pending **read-only** command (never a modifying or disruptive one) |
| Ctrl+Shift+K | Focus the chat box |
| ↑ / ↓ in the chat box | Recall earlier messages |
| Enter / Shift+Enter | Send / newline |
| Ctrl+Shift+C / V | Copy / paste in the terminal |

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

Inside a case directory: `case.json` (name, sensitivity, notes), `events.jsonl` (audit log),
`state.json` (conversation, chat and queue, rewritten on every change so the case can be
resumed), `term-<session>.log` (transcripts), `transcript.md` and `ticket-summary.md` (exports).

## Untrusted output

Everything a command prints is untrusted: a log line, a banner or a web page can carry text
meant to steer the model. Three layers deal with that:

- the system prompt tells the model to treat output as data;
- the review dialog flags text that looks like an injection before you send it;
- the model's reply is rendered with no remote resources at all (no images, media or
  embeds, and a Content-Security-Policy that only allows this origin), so a steered model
  cannot leak data by making the page fetch a URL. Links open in your browser.

Local risk rules raise a command to **disruptive** when it fetches and runs code
(`curl … | sh`, `bash <(wget …)`, `base64 -d | sh`, `Invoke-Expression`, `-EncodedCommand`,
`DownloadString`), erases (`shred`), opens a reverse shell or sets world-writable
permissions, and to **modifying** for `eval`, inline interpreter code (`python -c`) and
switching user (`su`, `sudo -i`).

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The core lives in `engine.py` and is frontend-agnostic. The GUI is `server/` plus `web/`,
and a Textual/tmux TUI is planned on the same core.

## Roadmap

- Connectors: ConnectWise (push ticket notes and transcripts) and Confluence (pull site
  notes, push runbooks). Outbound and read-only respectively, keys in the keyring.
- TUI (Textual + tmux, Linux)
- Intel TDX / NVIDIA attestation for `TEE/` models (SealedLore's `tee.py`, `dcap.py`, `nras.py`)
- IP/hostname pseudonymisation with local reverse mapping
- Per-client notes library
- Context compaction for long sessions (the usage indicator is the stop-gap)
