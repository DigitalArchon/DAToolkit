# DA Toolkit manual

This is the full reference. For what DA Toolkit is and how to install it, see the
[README](README.md).

> **Beta.** DA Toolkit is beta software and is not intended for production use. Use it at
> your own risk.

## Contents

- [First run](#first-run)
- [Workflow](#workflow)
- [Generation settings](#generation-settings)
- [Model tiers and end-to-end encryption](#model-tiers-and-end-to-end-encryption)
- [SSH, WinRM, RDP and windows](#ssh-winrm-rdp-and-windows)
- [Where things are stored](#where-things-are-stored)
- [Untrusted output](#untrusted-output)

## First run

DA Toolkit opens in its own window, which uses your system's WebKitGTK (see the
[README](README.md#install) for the package). If that isn't installed, or there's no graphical
display, it opens in your default web browser instead, with a banner saying why and what to
install. To always use the browser, set **Settings → General → Open DA Toolkit in** (it applies
from the next start); from a terminal, `datoolkit --browser` or `--window` overrides that for one
run, and `--no-open` just prints the local URL. In the browser, use **Quit** in the top bar to end
DA Toolkit: closing the tab leaves it running. The app window's clipboard and Save dialog are
the system's own; in the browser they're the browser's.

1. **Start a case.** The app offers this when it opens. Choose **Not now** to set it up
   first: settings, providers, models and hosts work without a case, and nothing is saved as a
   case until you start one (from **No case** in the top bar, or by sending a message or
   opening a session, which ask for one). Give it a name or ticket number and pick a sensitivity:

   | Sensitivity    | Allowed model tiers        | Use for                              |
   |----------------|----------------------------|--------------------------------------|
   | Open           | Standard, TEE, E2EE, Local | Trials, lab work, non-confidential   |
   | Confidential   | E2EE, Local                | Client data is involved              |
   | Sovereign      | Local only                 | Data must not leave the network      |

   If client data turns up in an Open case, raise it (click the sensitivity badge, or **✎**).
   A case's sensitivity can only go up: what was already sent stays with the models it went
   to, and a lower level would let more models see the conversation so far.

2. **Settings → AI providers → Add NanoGPT.** Paste your API key (it goes to the keyring),
   then click **Save & test**. If Claude Opus 5.5 is available it becomes the default model.
   NanoGPT's end-to-end encrypted `private/…` models (e.g. `private/glm-5-3`) are listed too.
3. **+ Session** opens a local shell or a saved host (add hosts under **Settings → Hosts**).
4. Describe the problem in the chat.

## Workflow

- **Layout.** Drag the bars between the chat, the terminals and the queue to resize them.
  On a small screen, **«** at the left of the terminal tabs hides the chat to a narrow strip
  (click the strip, **»**, or Ctrl+Shift+K to bring it back; a dot on the strip means the AI
  has written something since). **▾** on the queue header hides the queue list but keeps the
  header, with its counts and **Send results**. Attaching a photo or screenshot shows the
  chat again. Sizes and hidden panes are saved in the config and come back next launch.
- The chat is a conversation: the AI says what it makes of each result, what it wants to check
  next and why, and asks you what no command can tell it (when it started, what changed).
  Its questions come with quick-reply buttons that fill your reply; press Enter to send.
  If results are ready too, **Results (n)** beside Send opens the results review with your
  reply as its message, so answers and results go in one turn.
  Each proposal also appears as a card in the AI's message, with Run / Skip… / Force skip,
  and hypothesis moves (▲ ▼ ✓ ✕) are shown inline.
- The AI can change its mind: it withdraws pending commands it no longer wants (wrong syntax
  for the shell, superseded) and reorders the rest. Withdrawn items leave the queue (tick
  **show done** to see them) and **Restore** puts one back. The AI sees the queue, so it
  knows what is still pending and what has run but not been sent.
- If a model replies with commands but no message (thinking models sometimes go straight from
  reasoning to tool calls), DA Toolkit asks it once for the message, offering it no tools that
  time so it can only write.
- If a model's message announces commands ("I'll queue a few checks", "The fix is to turn on…",
  "Let's get a baseline first") but it queued nothing, or asks you to run items that are no
  longer pending (skipped, withdrawn or already run), DA Toolkit asks it once to queue them or
  correct itself. A model that writes its tool call into the message as text (some smaller
  and local models print `<tool_call>…` markup) has it read back as a real call; the markup is
  taken out of the message and the commands go to the queue like any other.
- A queued command with a placeholder in it (`<NAS_IP>`, `PATH/TO/file`, `x.x.x.x`) is sent back
  to the AI with a request to withdraw it and queue the real command; check such items before
  running them.
- **Web search**: the AI can search the web through NanoGPT (Perplexity by default; Valyu,
  Tavily, Kagi, Linkup, Brave, Sofya, Firecrawl or Exa in Settings → General) for advisories,
  release notes and exact syntax. By default each query appears in the AI's message for you to
  edit, approve or skip; "search without asking" applies to Open cases only, Confidential cases
  always ask, and Sovereign cases never search, because queries reach the search provider in
  the clear. Searches are billed to the NanoGPT key; each search card shows its cost.
  Providers differ a lot. Measured over three queries (September 2026), Perplexity found the
  most primary sources with about 1,300 characters of clean text per result; Valyu came next;
  Kagi finds good links but returns only a line of text per result, at $0.025 the dearest;
  Linkup returns long text but drifted into other languages; Exa returns titles only. Perplexity's
  results (tested again in October 2026 on eight support queries) were mostly the vendors' own
  documentation, about 2,000 characters of clean steps, menu paths and CLI per result; the AI
  gets up to 2,000 characters of each. When the chosen provider fails on NanoGPT's side
  (Perplexity was down in early October 2026), the search is made again with Valyu; when the
  account has Zero Data Retention required, which only Linkup is allowed under, with Linkup.
  The card says which ran.
- **Confidential cases search only with Linkup**, which keeps no data (zero data retention),
  whatever provider is chosen, for the AI and the research agent alike, and never fall back to
  another provider: if Linkup fails, the search fails.
- **Research agent.** A search returns only snippets; when the AI needs the documentation
  actually read, it hands a brief (product, exact version, what it needs, what you see) to a
  research agent, a second model (Claude Sonnet 5.5 by default, Settings → Model). The agent
  searches in one of two ways: for an answer (the search provider, Perplexity by default: a
  few results with substantial sourced extracts), or for links (the link finder, Kagi by
  default: the best pages fast, a line each, which it then reads itself); both are set in
  Settings → General, and the research card marks link searches. It can narrow a search to
  a vendor's own sites (e.g. `docs.sophos.com`; Kagi, Valyu and Linkup keep to them, Perplexity
  mostly, Brave not at all) and to pages from a date range, e.g. after a version's release
  (not with Kagi, which NanoGPT can't filter by date: that search runs without the dates and
  the agent is told). Every search, the AI's own included, leaves out video and social sites
  whose pages can't be read: YouTube, Vimeo, TikTok, Facebook, Instagram, X, Pinterest and
  LinkedIn. It reads the pages it finds through NanoGPT's scraper (vendor docs, release notes,
  knowledge-base articles, GitHub files), and reports back with the steps quoted and the
  sources numbered. The AI is told to call it whenever it isn't sure of the exact steps:
  products it knows less well, a version that may differ from what it knows, when you say the
  screen looks different from what it described, or when commands come back invalid. The
  research card in the AI's message shows each search and page as it happens, then the report.
  - It is gated like web search: by default you approve (and can edit) the brief first; in
    "search without asking" mode on an Open case it runs straight away; Confidential cases
    always ask; Sovereign cases never research. The agent sees only the brief, never the case,
    so edit out anything that identifies the client.
  - It can only fetch URLs that appeared in its search results or as links on pages it read,
    so a hostile page can't send it to a URL of its own making (a URL can carry data out).
    Budget per task: 6 searches, 12 pages, 10 minutes; at most 2 tasks per AI turn.
  - Pages that come back blocked (an anti-bot or challenge page, or a failed fetch) are
    fetched again in stealth mode automatically. Costs at the time of writing: $0.001 per page,
    $0.005 in stealth mode, plus the searches and the research model's tokens; the card shows
    the search and page costs.
  - The AI never fetches a page itself. When it needs one whole page (a long config reference,
    a source file), it asks the agent for that URL: the agent checks the page for text aimed
    at an AI, those passages are cut out (the AI is told how many), and the rest is returned
    in full (up to 40,000 characters). A URL that didn't come from a search result, a report or
    your own message is always asked about, even in "without asking" mode.
  - Reports are kept on this machine for 30 days. The same question about the same product
    version in a later case reuses the report (the card says "reused") instead of researching
    again; the AI can ask for a fresh one if it doesn't answer the question.
- **Retry.** When a request fails or you stop it, the status line (and the error note in the
  chat) offers **Retry with ‹model›**: the last message is sent again, once, with whichever
  model is selected now. So when a model is overloaded, pick another and retry; nothing needs
  retyping or re-attaching.
- The line above the chat box always says whose turn it is: *AI is responding* (with the
  phase, e.g. "Preparing commands", and a note if no text has arrived for a few seconds) or
  *✓ AI finished. Your turn*, with what is waiting for you.
- Proposed commands appear in the queue with a risk badge (read only / modifying /
  disruptive). Local rules can raise the AI's own risk label but never lower it. Disruptive
  commands need a second confirmation. A `>` or `;` inside quotes (an awk or jq program, a
  grep pattern) is not read as a redirect or a new command, unless the quoted text is handed
  to a shell again (`sh -c`, `ssh`, `xargs`, `find -exec` and the like). An awk program that
  writes a file or runs a command (`print > "file"`, `system()`) still counts as modifying.
  Commands that only list are read only: `iptables -S`, `nft list ruleset`, `mount` with no
  target, `net user`/`net share` with no switches, `dd … of=/dev/null`, `New-Object`, `Start-Sleep`.
- A separate **sensitive** badge marks commands that may expose secrets or private data,
  whatever their risk level: reading key files, `/etc/shadow`, `.env` and credential files,
  dumping the environment, shell history, secret stores, databases or a device
  configuration, and commands that carry a password in their own text (where it lands in
  shell history, the process list and the case log). Sensitive items aren't run by
  Ctrl+Shift+Enter, and their output gets a reminder to check it before it is sent. The AI is
  told which of its commands were flagged.
- **Run** types the command and presses Enter. **Insert** types it without Enter, so you can
  finish editing it in the shell. You can edit the command text in the queue before either.
- **What you see is what runs.** Characters the queue can't show faithfully are taken out of
  every command before it is queued: control characters (an escape sequence can end the
  shell's bracketed paste), bidi overrides that reorder text, zero-width and Unicode tag
  characters, and look-alike spaces (made plain spaces). Line breaks and tabs stay. The item
  then says what was removed, and the AI is told. From the AI, that is a sign of prompt
  injection.
- **Skip…** takes an optional note that is passed to the AI (e.g. "not allowed on prod").
  **Force skip** skips in one click; the AI is told you chose not to run it, with no reason.
- **Send results** collects each command's output from the terminal (from where you ran it
  to where the next one starts), redacts and truncates it, and shows it for review. The AI
  receives only what you send. Whatever you've typed in the chat box (and any attached
  photo) is sent with it, and the AI's open questions are shown with their quick replies,
  so you can answer them there. Cancel puts your message back in the chat box.
- **Send terminal selection** sends any highlighted terminal text.
- If the terminal buffer no longer has a command's output (page reloaded, session closed,
  case resumed), the output is taken from the session's transcript file instead. The
  review dialog says when this happened.
- The review dialog also warns when captured output looks like a prompt injection (text
  that tries to give the AI instructions). The AI is told to ignore such text, but you see
  it first and can edit it out.
- **Resume a case**: the case dialog lists earlier cases. Opening one restores the
  conversation, chat and queue; open sessions carry over.
- **Delete cases**: in the same list, **Delete** removes a case from disk (conversation,
  queue, audit log, transcripts, request log, images, runbook), or tick several and use
  **Delete selected**. The filter box narrows the list. The open case can't be deleted;
  exports saved elsewhere are left alone.
- **Edit a case**: **✎** beside the case name in the top bar (or a click on the sensitivity
  badge) edits the open case's name, notes and sensitivity; **Edit** in the case list does
  the same for an earlier case. Sensitivity can only be raised, and not while the AI is
  answering; if the selected model isn't allowed at the new level, choose another. The AI
  sees the new name and notes from your next message. The case folder keeps its original
  name, and every change is recorded in the audit log.
- **Context ▾**, next to the model, shows `Context 23k / 200k`: the prompt size of the last
  request (kept with the case, so a resumed case shows it too), against the selected model's
  context window when that is known. It turns amber past the threshold in **Settings →
  General** (default 100k) or at 75% of the window, whichever comes first, which is the cue to
  export a ticket summary and start a fresh case; red at 90% of the window means the next
  request may not fit. The window comes from NanoGPT's model details, from Settings →
  Providers → "Context window" (set it for local models to match the server, such as Ollama's
  `num_ctx`), or from the error the first time a request is too long.
- When a request is too long for the model, the chat says so with **What the AI knows…** and
  **Retry**: compact or remove earlier exchanges, then retry, or pick a model with a larger
  window. Some local servers (Ollama) cut an over-long prompt silently instead of refusing it;
  set their context window override so the figure warns in time.
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
Save baseline**. Later, run the same recipe and open **Saved baselines: diff or delete**: it
lists the host's baselines, newest first, with the case each was taken in. **Diff** gives a
unified diff per section (services, ports, routes, disks, packages, firewall, cron, users, …)
that you can send to the AI; pick one taken while the machine was healthy. **Delete** removes
a baseline, for example one saved by mistake on a broken machine. Baselines belong to the
host, not the case: deleting a case leaves them. They live under
`~/.local/share/datoolkit/baselines/<host>/`.

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
  real item; its tooltip shows the rehearsal. Commands with no known rehearsal have no button,
  and each item is rehearsed once at a time.
- **Second opinion.** **2nd opinion** on any state-changing or sensitive item, or from the
  disruptive confirmation, asks a reviewer model what could go wrong, whether it exposes
  sensitive data, and for a verdict (proceed / proceed with care / do not run). Set a
  different model under **Settings → Model** (it must be allowed by the case sensitivity);
  the reviewer sees only the command and the case notes, never the proposer's reasoning.
  **Automatic review** (off / disruptive commands / everything flagged) asks it in the
  background as each command is queued, and the verdict shows on the item; click it for the
  full review. It only adds warnings: a "do not run" verdict asks for confirmation before a
  non-disruptive command runs, but a review never lowers a risk level or clears a flag.
  Automatic reviews run only on the chosen reviewer, never the chat model.
- **Paired probes.** When the AI gives two commands the same `group` (a capture on one side,
  a ping from the other), **Run group** types them into their sessions at the same moment and
  the results carry start times.
- **Watch.** **Watch** on a read-only item wraps it in a bounded loop (every N seconds, M
  samples). When you send the result, iterations identical to the previous one are dropped.

### Photos, replay, context

- **📷** (or paste an image) attaches a photo of a screen, LED panel or label to the next
  message. Photos are resized locally, stored in the case directory and sent to the model
  under the same sensitivity gate as text.
- **Screenshot → chat** (or 🖥 by the chat box) attaches a picture of the active session:
  a terminal's screen drawn from its buffer with its colours, or the remote desktop of an
  RDP session. Use it when a login lands on an appliance menu (OPNsense, pfSense, Sophos)
  rather than a shell: the layout makes that obvious where plain text may not.
- **Vision.** The model picker marks models that read images (👁), text-only ones, and
  reasoning models (🧠), from NanoGPT's model details (Private Mode models borrow their public
  twin's; other providers can be marked under Settings → Providers → "Reads images"). With a
  text-only chat model, set a **vision helper** in Settings → Model: it describes each image
  once (text transcribed exactly, then the rest) and the chat model gets the description,
  shown under the image in the chat. That adds a request per image, so replies with images
  are slower. The helper (and the second-opinion reviewer) is chosen with the same searchable
  picker as the chat model; the helper's lists only vision models. An end-to-end encrypted
  helper, or a TEE one, is attested like the chat model before any image goes to it, and its
  state shows in the top bar and Settings → Model. With neither a vision model nor a helper,
  every image feature is disabled and says why; the top bar shows
  the current state (👁, 👁 via helper, or "no images").
- Screenshots are drawn at a whole-number 2× scale, cropped to the rows in use, with plain
  (not sub-pixel) text smoothing, and are not resampled again unless over 2048 px.
- **Every image goes through a redaction editor first.** Drag over anything that shouldn't
  reach the AI and it becomes solid black; Undo and Clear are there, Attach sends. The black
  replaces the pixels in the one bitmap (no layers; boxes snap outwards to whole pixels so no
  edge is half-blended), so the unredacted original is never uploaded or stored.
- **Images carry nothing but pixels.** The server decodes every attached image and re-encodes
  it from its pixels alone, so EXIF (GPS, camera, times), the embedded EXIF thumbnail, XMP,
  ICC profiles, PNG text chunks, JPEG comments, extra APNG frames and bytes appended after the
  image are all dropped, whatever the page sent. Only that cleaned image is stored (as a
  generic `img-N.png` / `img-N.jpg`) and sent; the original file name never leaves the page.
- **Exports are saved where you choose**: the app window opens the system Save dialog (in
  `--browser` mode the browser downloads the file). A copy still goes into the case folder.
  - **Export transcript (Markdown)** is the readable case record.
  - **Full export** is a ZIP with every request DA Toolkit made to a model, in order: the
    system prompt (in full whenever it changed), the messages sent, the model's reasoning as
    the provider returned it, and its reply with tool calls (`full-transcript.md`); the images
    exactly as the model received them; the AI-written documents; and the raw data (request
    log, conversation, chat, queue, hypotheses, audit log). Raw terminal transcripts are
    optional because they are unredacted. Cases started before request logging existed get
    their earlier conversation reconstructed, marked as such.
  - Ticket summary, client update and runbook have **Save as…** next to Copy.
- **Context ▾ → Timeline replay** plays the case back: events on the left, the terminal
  transcript on the right, on one slider.
- **Context ▾ → What the AI knows** shows the exact context the model gets next turn, with a
  size estimate per exchange, and lets you remove exchanges from it. The chat and the audit
  log keep them.
- **Compact** (in the same dialog) has the chat model summarise the older exchanges, by
  default all but the last two, which stay word for word. The summary keeps the problem,
  findings with their evidence, every change made to a system, what was ruled out, your
  instructions and open threads, with hostnames, addresses and errors copied exactly. You
  see it and can edit it before **Apply**; nothing changes until then. **Cancel** while it is
  being written stops the request at the provider. The request uses the
  chat model, so the case's sensitivity rules apply as usual, and with Claude it reads the
  conversation from the prompt cache while that is warm. Applying changes the start of the
  conversation, so the next turn writes the shorter context to the cache once (see Prompt
  caching). After a request that was too long, only the part to summarise is sent. The
  previous context is saved in the case folder (`context-before-compact-N.json`), and
  **Undo last compaction** puts it back while the summary still opens the conversation;
  exchanges added since are kept.
- **Export ▾ → Client update** writes the plain-language version of the ticket note.
- **Export ▾ → Distil runbook** turns a solved case into `runbook.md`. When a new case's
  first message resembles a past case, the similar cases are noted in the chat and their
  runbooks go into the AI's context as leads. **Context ▾ → Similar past cases…** searches by
  hand, and **Context ▾ → Open case folder** opens the case's files.

### Companion view

**📱** in the top bar serves a phone-sized, read-mostly page on the LAN: the AI's last message,
the hypothesis board and the queue, with "I ran it" and "Skip", and a camera button to send
the AI a photo. Use it when you are typing commands at a console away from the laptop (a
server room, a switch's serial port) and want the next step in your hand.

- **Read aloud.** "Read new AI messages aloud as they arrive" starts with the *next*
  message; what's already on screen isn't read. **▶ Read** reads the current message on demand.
  While it reads, the button is **⏸ Pause** (then **▶ Resume**, which picks up from the start
  of the sentence it stopped in) and **⏹ Stop** ends it. Switching it on says a short
  confirmation, which is also what lets an iPhone speak later.
- **Photo to AI.** Take a photo (or choose one), usually of the screen you are working at,
  drag over anything sensitive to black it out, type what it is, and **Send to AI**. It goes
  into the case like a photo attached on the computer: the same case-sensitivity and vision
  rules, cleaned of all metadata by the server, shown in the chat as "from your phone". The
  AI is told it came from your phone. It's disabled, with the reason, while there is no case,
  while the model can't use images, and while the AI is responding.

**Port and firewall.** The companion always listens on the same port, 48443 unless you change
it in **Settings → General**, so with a firewall on you only open that one
(`sudo ufw allow 48443/tcp`). The main window stays on 127.0.0.1 and is never on the LAN.
A request without the phone's token is refused on its headers, before any of its body is read,
so nobody on the network can tie the app up by sending it data.

**Pairing takes two scans**, so that nothing secret crosses the network before you have
checked who the phone is talking to:

1. **Check the certificate.** Press **Start** and scan the first code. It opens a page with
   no secret on it. The browser warns that the connection isn't private: DA Toolkit uses a
   self-signed certificate made for this install. Accept the warning, open the certificate
   details (Android Chrome: the icon left of the address → Certificate information; iPhone
   Safari: Show Details → view the certificate → More Details) and compare its **SHA-256
   fingerprint** with the one in the dialog, every pair of it. If anything differs, press
   **It doesn't match: stop**: someone may be intercepting traffic on that network.
2. **Connect.** Press **Fingerprint matches** and scan the second code in the same browser.
   It carries the token. The browser has already accepted this exact certificate, so it must
   not warn again. If it does, don't continue: go back to step 1 and check the certificate.

**A phone that is already paired** can skip straight to step 2 (**Already paired: skip**).
Its browser accepted this certificate before, so it connects without a warning. If it warns
anyway, treat it like any other warning and go back to step 1: browsers forget accepted
certificates after a while, and a new phone or someone in the middle produces the same
warning. The browser shows that warning before it sends the request, so refusing it keeps the
token on this machine.

The certificate is kept (`~/.config/datoolkit/companion-cert.pem`, key readable only by you),
so its fingerprint stays the same and each phone checks it once (until its browser forgets). **New certificate** makes a new one (every
phone checks again); a new one is also made when it expires, after about two years.

- The token changes every time the companion starts. **New code** disconnects phones paired
  with the old one; **Stop** disconnects all of them. The button shows how many are connected.
- The token is only in the second code's `#` fragment, which the browser never sends in a
  request, and the page removes it from the address bar and history.
- The companion's port serves only the phone's page and its API: the case name, the last 30
  chat messages, the queue, the hypothesis board and the list of open sessions. It can mark
  queue items ran, skipped or pending (with a note), mark hypotheses, and send a photo with a
  description to the AI. It cannot reach a terminal or run anything, open or close sessions,
  see settings, prompts or credential dialogs, or send results or plain messages. Whatever
  the AI proposes in reply still needs your click on the computer.
- The chat does go to the phone, so think twice before pairing on a Confidential case.

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

`datoolkit-replay` runs today's risk, blast-radius, sensitive-data, redaction and injection rules over past
cases' event logs and prints what would now be classified differently. Use it after editing
the rules. From the AppImage, run `DAToolkit-<version>-x86_64.AppImage replay`.

### Keyboard shortcuts

| Keys | Action |
|---|---|
| Alt+1 … Alt+9 | Switch terminal tab |
| Ctrl+Shift+Enter | Run the next pending **read-only** command (never a modifying, disruptive or sensitive one, or one the reviewer said not to run) |
| Ctrl+Shift+K | Focus the chat box (showing the chat if it is hidden) |
| ↑ / ↓ in the chat box | Recall earlier messages |
| Enter / Shift+Enter | Send / newline |
| Ctrl+Shift+C / V | Copy / paste in the terminal |

## Generation settings

Settings → Model sets what goes with every request: temperature (default 0.3), reasoning
effort (default low), max output tokens, top-p, frequency and presence penalty, and seed.
Blank means the model's own default. Reasoning effort is sent only to reasoning models and is
moved to the nearest level each model accepts (Kimi and GLM take low / high / max; Opus low to
max). The same settings apply through NanoGPT Private Mode.

**Prompt caching** (Settings → Model, default on for 1 hour): Claude models through NanoGPT
(Open cases) reuse the conversation each request already sent, so later requests in a case
pay a tenth of the input price for everything before the newest message, and answer sooner.
Writing the cache costs extra (2× the input price for the 1-hour cache, 1.25× for 5 minutes);
1 hour outlasts the minutes spent running commands between replies, where a 5-minute cache
would expire. For the cache to hold, the changing part of the prompt (sessions, queue,
hypotheses) is sent after the conversation instead of in the system prompt; the full export
shows it as "Current state". The context figure above the chat shows how much of the last
request came from the cache. Other models cache automatically where their provider does; the
research agent and write-ups don't cache.

Within one reply, Claude Opus 5.5 through NanoGPT caches only up to your latest message: search
results and research reports the AI receives while it works are paid at the full input price
in each further step of that reply, then cached with your next message. (Sonnet caches them at
once.) The recipe list in the system prompt is always the full one, so opening a session
doesn't throw the cached conversation away.

**Model-specific instructions** (Settings → Model): text added to the end of the system prompt
when the chat model's id contains a match you choose (case doesn't matter, so `glm` covers
`z-ai/glm-5.3` and `private/glm-5-3`). DA Toolkit has built-in notes for models whose habits
were measured in its [model evaluation](docs/model-evaluation.md) (`tools/model-eval`); the settings show them and can
turn them off. Use your own to correct a model's habits, e.g. one that keeps describing
commands instead of queueing them.

[Which models work well](docs/model-evaluation.md) describes the evaluation: how nine models
did, the habits that tripped them up, and why each nudge and note exists.

## Model tiers and end-to-end encryption

| Tier | Detected from | What the provider can see |
|---|---|---|
| **Standard** | anything else | Everything |
| **TEE** | ids starting `TEE/` or `phala/` | The model runs in an attested enclave, but prompts pass NanoGPT's gateway **in the clear** |
| **E2EE** | NanoGPT `private/…` ids | Ciphertext only, plus your account, the model, timing, sizes and usage |
| **Local** | localhost / private-IP base URL | Nothing leaves your network |

**Local trusts your network.** A base URL counts as Local when its host is `localhost`, a
private or link-local IP, or a name ending `.local` or `.lan`, so Confidential and Sovereign
cases may use it. With `http://`, prompts (client data included) and the API key cross the
LAN unencrypted, and a `.lan` name is only as trustworthy as your DNS. Use Local over the
network only on a LAN you trust, or put the model server behind HTTPS.

Override any model's tier under **Settings → AI providers → Tier overrides**.

**Web search and the research agent are gated by approval, not by tier.** Search queries reach
the search provider in the clear whatever the chat model's tier. The research model works
from the brief alone (never the case), the same kind of text as a search query, so it may be a
Standard-tier model in a Confidential case: there, every brief waits for your approval and
edit. Sovereign cases do neither.

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

Like SealedLore, DA Toolkit doesn't use NanoGPT's `npx @nanogpt/private-mode@latest` proxy,
because that would let NanoGPT change the code that holds your plaintext.

Verifying an enclave contacts NanoGPT's relay (for the attestation bundle), Sigstore and
GitHub.

**How TEE attestation works** (`src/datoolkit/llm/tee.py`, `dcap.py`, `nras.py`, `ethsig.py`,
ported from SealedLore with the checks unchanged, and tested against a real attestation
captured from `TEE/glm-5.3-flash`):

1. **Attest before sending.** NanoGPT's `/tee/attestation` is asked for evidence bound to a
   fresh random nonce. Nothing goes to a TEE model (chat, vision helper, write-ups or the
   second-opinion reviewer) until that holds, and it is made again before a send once it is
   15 minutes old.
   - The Intel TDX quote's own bytes must bind the enclave's reply-signing key and our nonce
     (never NanoGPT's JSON copy of them). For per-instance providers (Chutes), each instance's
     key, bound into its quote, must have signed our nonce, and every instance must attest.
   - The quote is verified up to Intel's SGX root, which is pinned in the code: the PCK chain,
     the quoting enclave, the quote's signature, Intel's revocation lists, and Intel's signed
     TCB information. A debug TD, a revoked or unlisted TCB, or a forged chain is refused.
   - The GPU evidence goes to NVIDIA's attestation service. Its ES384-signed verdict must be
     for our nonce, current, and pass for every GPU (measurements, secure boot, debug off).
2. **Refuse** evidence relayed for a nonce the relay chose (replayable), no attestation at
   all (Tinfoil-hosted `TEE/` models; use their `private/` twin), or anything that fails
   the checks above. Nothing is sent.
3. **Partial** when nothing failed but something couldn't be checked or isn't current:
   Intel's collateral or NVIDIA's service out of reach, no GPU evidence offered, or a TCB
   status other than UpToDate. The top bar shows **🛡 TEE attested (partial)**, and the tooltip
   names each shortfall.
4. **Check each reply's signature.** Every reply's completion id is looked up at
   `/tee/signature/`, and the signer recovered from its EIP-191 signature must be the attested
   key. Replies are marked ✔ signed, ✖ signature mismatch, unsigned (the provider signs
   none) or signature unchecked.

What this doesn't prove: which software the enclave runs, or that a reply's text is byte for
byte what was signed (the signed record hashes the request and reply as NanoGPT's gateway saw
them). The prompt also passes NanoGPT's gateway in the clear, so TEE models stay out of
Confidential cases. Verifying contacts NanoGPT, Intel (`api.trustedservices.intel.com`,
`certificates.trustedservices.intel.com`) and NVIDIA (`nras.attestation.nvidia.com`). Intel's
collateral and NVIDIA's keys are cached for an hour.

## SSH, WinRM, RDP and windows

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
- **RDP** opens the remote desktop in a tab, through Apache Guacamole's `guacd`
  (it runs as a local service on 127.0.0.1:4822; see the install table in the README: it's
  `sudo apt install guacd` on Ubuntu and Mint, and upstream's container on Debian, Arch and
  CachyOS). If it isn't running, opening an RDP host shows the commands for your distribution
  in a dialog you can copy them from. DA Toolkit only connects to guacd on this machine, since
  that link isn't encrypted, and does the connection handshake itself, so the password never reaches the page. Add RDP hosts under Settings → Hosts with the security mode and keyboard layout.
  - **Certificates are trusted on first use and pinned.** Before every connection DA Toolkit
    reads the server's TLS certificate itself. The first time it shows you the subject,
    issuer and SHA-256 fingerprint to verify (on Windows: the Remote Desktop certificate in
    `certlm.msc`); after that, a different certificate blocks the connection until you forget
    the pin in Settings → Hosts. Servers offering only legacy RDP security have no
    certificate, so you are warned on every connection. guacd 1.3 cannot enforce a pin
    itself, so the check and guacd's connection are separate TLS sessions.
  - The toolbar sends Ctrl+Alt+Del, Win and Win+R (keys the app window can't capture), takes
    a screenshot to the chat, sends text you copied on the remote desktop to the AI, and puts
    text on the remote clipboard or types it.
  - **Size.** The desktop is drawn at its real size and scrolls when the tab is smaller, so
    resizing the window or the panes never disturbs the remote. When the size no longer
    matches, **Fit to window** lights up: one click resizes the remote to the tab. Servers
    that can't resize live (no display-update, e.g. before Windows 8.1 / Server 2012 R2) are
    reconnected at the new size instead, which resumes the same Windows session.
    **Maximize** hides the chat, the queue and the tabs so the desktop gets the whole window,
    and **Restore** brings them back (so does Ctrl+Shift+K). A desktop that fitted its tab is
    fitted again each way.
  - **Run/Insert types a queued command into whichever window has focus** on the remote
    desktop (confirmed once per session). Output isn't captured; in the Send results dialog,
    paste it, use "Copied text", or attach a screenshot.
  - Limits: no smart-card or USB redirection, text-only clipboard, one monitor.
- **Window sessions** work through a window on your own screen: typically a ScreenConnect
  control window, but TeamViewer, AnyDesk, a VM console or an iDRAC/iLO console work the
  same way. Nothing needs setting up on the remote side, and DA Toolkit needs no login or API
  for the tool.
  - **+ Session → Window on this screen…** lists the open windows, remote-support tools
    first. Pick one and give it a name (suggested from the title, e.g. the computer's name).
    The window is followed by its X window id, so moving or resizing it doesn't matter.
  - The AI can't see the window. **Screenshot → chat** captures just that window (not the
    rest of your screen), you black out anything sensitive, and it's attached to your next
    message. The tab shows a preview that stays on your machine.
  - The AI prefers commands to click paths and says which shell each one goes in
    (PowerShell or cmd, as admin or not). **Copy to run** puts the command on your
    clipboard and marks it run; paste it into the window (remote-support tools pass the
    clipboard through) and run it there. Nothing is ever typed into the window for you.
  - Send the result as a screenshot, or as text: copy the output in the window and use
    **Paste result → AI**, or **Copied text** in the Send results dialog. Text is exact
    where a screenshot may be misread, so for IDs, paths and long lists the AI ends the
    command with `| clip`, which puts its output on the clipboard.
  - Commands that would cut the session are flagged: stopping or uninstalling the
    remote-support agent (ScreenConnect, TeamViewer, AnyDesk, ...), logging off, and the
    usual network changes.
  - **X11 only.** Wayland doesn't let one program read another's windows. Under XWayland,
    X11 programs (the ScreenConnect client among them) may still be listed and captured,
    but this hasn't been tested. A window that's minimised or on another workspace can't be
    captured: bring it back first. Without a compositor (most desktops have one), the parts
    of the window that other windows cover can't be read either.
- **Linked sessions.** Sessions to the same address are linked automatically as one machine
  (a coloured bar on their tabs); 🔗 links sessions by hand (hostname vs IP, NAT) or unlinks
  one for good. The AI is told which linked session takes commands and which is your
  desktop view (an RDP or window session), so it sends commands to the shell while you
  watch the GUI.

## Where things are stored

| What | Where |
|---|---|
| Config (no secrets) | `~/.config/datoolkit/config.toml` |
| Pinned RDP certificates | `~/.config/datoolkit/rdp_pins.json` |
| Companion certificate and key | `~/.config/datoolkit/companion-cert.pem`, `companion-key.pem` (0600) |
| Secrets | OS keyring, service `datoolkit` |
| Case logs, transcripts, exports | `~/.local/share/datoolkit/cases/<case-id>/` |
| Research reports (reused for 30 days) | `~/.local/share/datoolkit/research/` |

Inside a case directory: `case.json` (name, sensitivity, notes), `events.jsonl` (audit log),
`state.json` (conversation, chat and queue, rewritten on every change so the case can be
resumed), `requests.jsonl` (every model request: prompt, messages, reasoning, reply),
`img-N.png/jpg` (images as sent), `term-<session>.log` (transcripts), `transcript.md` and
`ticket-summary.md` (exports).

## Untrusted output

Everything a command prints is untrusted: a log line, a banner or a web page can carry text
meant to steer the model. Three layers deal with that:

- the system prompt tells the model to treat output as data;
- the review dialog flags text that looks like an injection before you send it;
- web search results, research reports and fetched pages reach the model marked as untrusted,
  with the same check; the research agent works from the brief alone and fetches only URLs
  it was shown, and a page returned whole has first been checked by the agent;
- the model's reply is rendered with no remote resources at all (no images, media or
  embeds, and a Content-Security-Policy that only allows this origin), so a steered model
  cannot leak data by making the page fetch a URL. Links open in your browser.

Local risk rules raise a command to **disruptive** when it fetches and runs code
(`curl … | sh`, `bash <(wget …)`, `base64 -d | sh`, `Invoke-Expression`, `-EncodedCommand`,
`DownloadString`), erases (`shred`), opens a reverse shell or sets world-writable
permissions, and to **modifying** for `eval`, inline interpreter code (`python -c`) and
switching user (`su`, `sudo -i`).
