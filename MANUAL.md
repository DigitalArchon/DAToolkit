# DA Toolkit manual

This is the full reference. For what DA Toolkit is and how to install it, see the
[README](README.md).

> **Beta.** DA Toolkit is beta software and is not intended for production use. Use it at
> your own risk.

## Contents

- [First run](#first-run)
- [Workflow](#workflow)
- [AI providers](#ai-providers)
- [Generation settings](#generation-settings)
- [Model tiers and end-to-end encryption](#model-tiers-and-end-to-end-encryption)
- [SSH, WinRM, RDP and windows](#ssh-winrm-rdp-and-windows)
- [Where things are stored](#where-things-are-stored)
- [Untrusted output](#untrusted-output)
- [Security and privacy](docs/security-and-privacy.md), a document of its own: what leaves this
  computer, every service DA Toolkit contacts, and its limits

## First run

DA Toolkit opens in its own window, which uses your system's WebKitGTK (see the
[README](README.md#install) for the package). If that isn't installed, or there's no graphical
display, it opens in your default web browser instead, with a banner saying why and what to
install. To always use the browser, set **Settings → General → Open DA Toolkit in** (it applies
from the next start); from a terminal, `datoolkit --browser` or `--window` overrides that for one
run, and `--no-open` runs it for the browser and just prints the local URL (with `--window` it is
refused, saying which to leave out). `--port 48500` has the app listen on a port you choose
instead of a free one picked at each start; it is on 127.0.0.1 either way. The browser is
opened on a one-time code, not
the app's token, since what a browser is opened with is on its command line for every user of
the computer to read: the page exchanges the code once, within two minutes, for the token, and
only a program of yours can. The address printed to open by hand carries the token after a
`#`, which the browser never sends. In the browser, use **Quit** in the top bar to end
DA Toolkit: closing the tab leaves it running. If the page loses its connection to DA Toolkit, a
banner says so until it is back: what is shown may be out of date, and nothing can be run or
sent meanwhile. A page left open from an earlier start says that its access token was refused:
close it and open DA Toolkit again. Nothing dropped on the app window replaces the
app: a file or a link dropped on it is refused (text dropped into a field still goes in). The
app window's clipboard and Save dialog are
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

2. **Settings → AI providers → Add NanoGPT** (or Anthropic, OpenRouter, WaveSpeed, a local
   server, or any other OpenAI- or Anthropic-compatible service: see [AI providers](#ai-providers)).
   Paste your API key (it goes to the keyring), then click **Save & test**. If Claude Opus 5.5
   is available it becomes the default model. NanoGPT's end-to-end encrypted `private/…`
   models (e.g. `private/glm-5-3`) are listed too. Web search and the research agent go
   through NanoGPT, so add it even when the chat model is elsewhere.
3. **+ Session** opens a local shell or a saved host (add hosts under **Settings → Hosts**).
4. Describe the problem in the chat.

**Two DA Toolkits at once.** DA Toolkit has one case open at a time, so with two tickets you may
start it twice. That works, a case each, and the second says as it starts (in a banner, and on
the terminal) that another is running with the same settings. Three things keep them from
undoing each other's work:

- **A case is open in one of them at a time.** Opening a case the other has open is refused
  ("This case is open in another DA Toolkit; it would overwrite that one's conversation."), and
  so are editing or deleting it, and renaming or deleting a client one of whose cases it has
  open.
- **Settings are saved by one of them.** Each read `config.toml` when it started, and a save
  writes the whole file. Once one of them has saved anything (a provider, a host, a client, a
  setting, the model chosen, even a pane dragged to another size), the other's copy is behind
  the file, and a change to the settings made there is refused whole, the keyring untouched:
  "Settings were changed by another DA Toolkit that is running. Close this one and start it
  again to pick them up; nothing was saved here." The pane sizes and the model you choose in
  the one that is behind still hold there until it is closed, without being saved (a notice
  says so once for the model). A `config.toml` edited by hand while the app runs is treated
  the same way: it is not overwritten.
- **The guacd container is shared.** Where DA Toolkit runs guacd's container for RDP, both use
  the one container, and it is removed when the last of them to use it closes its last RDP
  session or quits.

## Workflow

- **Layout.** Drag the bars between the chat, the terminals and the queue to resize them.
  On a small screen, **«** at the left of the terminal tabs hides the chat to a narrow strip
  (click the strip, **»**, or Ctrl+Shift+K to bring it back; a dot on the strip means the AI
  has written something since). **▾** on the queue header hides the queue list but keeps the
  header, with its counts and **Send results**. Attaching a photo or screenshot shows the
  chat again. Sizes and hidden panes are saved in the config and come back next launch.
  At the app window's smallest size (900×600) the top bar wraps as a whole, a long case or
  model name is cut with an ellipsis (hover the case's for all of it), and a browser window too
  small for everything scrolls as a page, so no pane lies over another. Notices appear above the
  queue, each with a **×** to dismiss it.
- The chat is a conversation: the AI says what it makes of each result, what it wants to check
  next and why, and asks you what no command can tell it (when it started, what changed).
  Its questions come with quick-reply buttons that fill your reply; press Enter to send.
  If results are ready too, **Results (n)** beside Send opens the results review with your
  reply as its message, so answers and results go in one turn.
  Each proposal also appears as a card in the AI's message, with Run / Skip… / Force skip,
  and hypothesis moves (▲ ▼ ✓ ✕) are shown inline.
- The AI can change its mind: it withdraws pending commands it no longer wants (wrong syntax
  for the shell, superseded) and reorders the rest. It can do that only to what it queued
  itself, a recipe it queued included: the steps of a recipe you queued, a tool transfer and
  its hash check, a rollback and a dry run stay where you put them, and the AI is told they are
  yours. Withdrawn items leave the queue (tick **show done** to see them) and **Restore** puts
  one back. The AI sees the queue, so it knows what is still pending and what has run but not
  been sent.
- If a model replies with commands but no message (thinking models sometimes go straight from
  reasoning to tool calls), DA Toolkit asks it once for the message, offering it no tools that
  time so it can only write.
- If a model's message announces commands ("I'll queue a few checks", "The fix is to turn on…",
  "Let's get a baseline first") but it queued nothing, or asks you to run items that are no
  longer pending (skipped, withdrawn or already run), DA Toolkit asks it once to queue them or
  correct itself. A model that writes its tool call into the message as text (some smaller
  and local models print `<tool_call>…` markup) has it read back as a real call when the markup
  ends the message, on a line of its own and outside a code block: the commands go to the queue
  like any other, and a note in the message says what was taken out ("[A tool call written here
  as text was read as a call: propose_commands]"). Markup a model only quotes (in the middle of
  a sentence, in backticks or a code block, from a log or a page it read) stays in the message
  as text, and a Claude model's is never read as a call: Claude makes its calls as calls.
- A queued command with a placeholder in it (`<NAS_IP>`, `PATH/TO/file`, `x.x.x.x`) is sent back
  to the AI with a request to withdraw it and queue the real command; check such items before
  running them.
- **Web search**: the AI can search the web through NanoGPT (Perplexity by default; Valyu,
  Tavily, Kagi, Linkup, Brave, Sofya, Firecrawl or Exa in Settings → General) for advisories,
  release notes and exact syntax. By default each query appears in the AI's message for you to
  edit, approve or skip; "search without asking" applies to Open cases only, Confidential cases
  always ask, and Sovereign cases never search, because queries reach the search provider in
  the clear. Secrets are blanked out of a query first. A query that says who or where a
  computer is waits for you even when searching without asking, and its card says what it
  names: an e-mail address, a MAC address (as Cisco and HP write one too), an IP address that
  isn't a public one (IPv4 or IPv6), the server of a `\\server\share` path, this computer's own
  names (your login and real name, the host name), the case's client, and the host, domain and
  login of each open session to another machine. A name is found as it may be written: `dc01`
  for `dc01.acme-corp.local`, `acme_ws01` or `acme ws01` for `ACME-WS01`, the user or the domain
  of `ACME\jsmith`. Words too common to name anyone (`root`, `administrator`, `firewall`,
  `server`) are not looked for. Characters no one can see (a zero-width space inside
  a key or an address) are taken out before either check, and what was checked is what is sent.
  Searches are billed to the NanoGPT key; each search card shows its cost.
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
- **Anthropic's own search** (Settings → General → Searches for the chat model): when the chat
  model is a Claude model on an Anthropic provider, Claude can search with Anthropic's web
  search instead. It runs inside Claude's request, on Anthropic's side, so **no query can be
  shown to you first**: Claude searches as it works, up to 4 searches a turn (as the gated
  search allows), billed by Anthropic. It is used only for Open cases; Confidential cases use
  the gated NanoGPT search
  (or none, without NanoGPT), and Sovereign cases never search. Its searches still show on the
  AI's message ("Anthropic (unasked)"), with their results, and the sources Claude cites are
  listed under its reply (and in the exported transcript). The top bar shows **🔎 Anthropic
  search** while it is in use. Research, which reads whole pages, still goes through the gated
  research agent on NanoGPT; without a NanoGPT provider the AI isn't offered research.
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
    so edit out anything that identifies the client. A brief (or a page's address) that names
    what a search is held for (an address, this computer's names, the case's client, a
    session's host or login: see Web search above) waits for you in every mode. The agent's
    own searches, which nobody is asked about, are refused when they name one, except what the
    brief you approved names itself.
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
  - Reports are kept on this machine for 30 days, each with the client and the sensitivity of
    the case it was written for. The same question about the same product version in a later
    case reuses the report (the card says "reused") instead of researching again; the AI can
    ask for a fresh one if it doesn't answer the question. A brief is written from a case, so
    a report is reused only within one client (or between cases that have none), and only by a
    case at least as sensitive as the one it came from. Where briefs wait for your approval,
    the kept report is looked up after you have approved.
- **Retry.** When a request fails or you stop it, the status line (and the error note in the
  chat) offers **Retry with ‹model›**: the last message is sent again, once, with whichever
  model is selected now. So when a model is overloaded, pick another and retry; nothing needs
  retyping or re-attaching.
- **A reply that didn't arrive whole.** When the connection closes before the provider has said
  the reply is complete (a proxy's time limit, a server that crashed), the turn ends as an
  error with Retry: what arrived stays on screen, and no command or other call in it is taken.
  A reply the provider ended early itself gets a note under it, "The reply was cut off at the
  output limit." or "… by the provider's content filter.": the calls that were written whole
  are carried out, and the one being written when it stopped is not. A turn that ends with
  nothing to show (an empty reply, or one cut off before any of it came) says so and offers
  Retry. One turn has six rounds (each search, and each time DA Toolkit asks the model again,
  takes one); a turn that uses them all says that it stopped there, and your next message lets
  it carry on.
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
  whatever their risk level: reading key files, `/etc/shadow`, `.env` and credential files
  (NetworkManager's connections, Windows paths too, and `find … -exec cat`), a computer's own
  SSH settings, keys, browser data, saved passwords, mail and messages, listing personal folders
  (`~/Documents`), dumping the environment, shell history, secret stores, databases or a device
  configuration, and commands that carry a password in their own text (where it lands in
  shell history, the process list and the case log). A public key (`.pub`) is no secret. Sensitive items aren't run by
  Ctrl+Shift+Enter, and their output gets a reminder to check it before it is sent. The AI is
  told which of its commands were flagged.
- **Run** types the command and presses Enter. **Insert** types it without Enter, so you can
  finish editing it in the shell. You can edit the command text in the queue before either.
  A command with several lines is pasted as one block: shells with bracketed paste (bash 5.1
  and later, zsh, fish, PowerShell 7) hold it for you to edit, but a device CLI or an older
  shell runs each line as its line break arrives, so there use **Run**, or **Insert** only
  single-line commands.
- **What you see is what runs.** Characters the queue can't show faithfully are taken out of
  every command before it is queued: control characters (an escape sequence can end the
  shell's bracketed paste), bidi overrides that reorder text, zero-width and Unicode tag
  characters, and look-alike spaces (made plain spaces). Line breaks and tabs stay. The item
  then says what was removed, and the AI is told. From the AI, that is a sign of prompt
  injection.
- **The command box shows all of its command, and Run types that command.** The box grows
  with its text up to 400 px (less in a low window) and scrolls past that, with a scroll bar
  that is always drawn. A command of several lines says how many under the box, and one too long for
  the box says so there ("62 lines, 5.2 MB: scroll the box to read all of it"); the command
  on a card in the chat follows the same rule. **Run**, **Insert**, Ctrl+Shift+Enter and **Run
  group** first save what is in the box and then type the command as the server holds it: if
  the item is no longer the one you were shown (it was edited in another window), nothing is
  typed and it is pending again. An emptied box runs nothing: Run and Insert refuse it and say
  to skip the item instead, and Escape in the box brings the saved command back. Rows move
  while the AI works (it withdraws and reorders items, a reviewer's verdict makes a row
  taller), so a click that lands on a row which moved there less than half a second ago does
  nothing, and the row flashes.
- **Skip…** takes an optional note that is passed to the AI (e.g. "not allowed on prod").
  **Force skip** skips in one click; the AI is told you chose not to run it, with no reason.
- **Send results** collects each command's output from the terminal (from where you ran it
  to where the next one starts), redacts and truncates it, and shows it for review. The AI
  receives only what you send. Whatever you've typed in the chat box (and any attached
  photo) is sent with it, and the AI's open questions are shown with their quick replies,
  so you can answer them there. Cancel puts your message back in the chat box.
- **What redaction blanks, and what it doesn't.** Redaction looks for secrets that something
  names. It blanks the value of a setting whose name says password, secret, token, key or
  credential (`DB_PASS`, `bindpw`, `api_key`; Windows' wording in German, French and other
  languages too), whole, in `name=value` and `name: value` lines, JSON, YAML, XML (a pfSense
  `config.xml`, a Wi-Fi profile), connection strings and addresses (`https://user:pass@host`,
  `?key=…`); the secrets in device configurations (Cisco, Junos, FortiGate, HP); passwords on
  the command lines of common tools (`mysql -p…`, `sqlcmd -P`, `net use`, `sshpass -p`,
  `--password`); private keys, also in base64; password hashes; `Authorization` and `Cookie`
  headers; a BitLocker recovery key; and the tokens of well-known services by their shape.
  Each becomes `[REDACTED]` (or `[REDACTED PRIVATE KEY]`, `[REDACTED HASH]`), and the review
  dialog says how many were made. It does not blank a secret that nothing names: a bare base64
  string, a password in a table's column or in a sentence, `user:password` on a line of its
  own. Nor does it touch names, addresses, host names or other personal data. It is a safety
  net: read what you send, and edit out the rest.
- A password you type into a queued command (`sshpass -p …`, `mysql -p…`) is blanked the same
  way wherever that command goes to a model: in the header of its result, in the queue the AI
  sees, in a second opinion. The queue on your screen, the audit log and what is typed keep
  the command as you wrote it.
- **Send terminal selection** sends any highlighted terminal text.
- If the terminal buffer no longer has a command's output (page reloaded, session closed,
  case resumed), the output is taken from the session's transcript file instead. The
  review dialog says when this happened. At most 4 MB of one command's output is read from the
  transcript, its start and its end, with a line between saying how much was not read.
- The review dialog also warns when captured output looks like a prompt injection (text
  that tries to give the AI instructions). The AI is told to ignore such text, but you see
  it first and can edit it out.
- **Resume a case**: the case dialog lists earlier cases. Opening one restores the
  conversation, chat and queue; open sessions carry over. A case whose saved state can't be
  read (a crash or a full disk cut the file short) opens from the copy kept of the save before
  it, and a note in the chat says that the last change may be missing; the unreadable file
  stays in the case folder. A case whose `case.json` is lost comes back at the highest
  sensitivity its audit log ever recorded for it, and as Sovereign where the log can't say.
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
- **Clients.** A case can belong to a client (a customer), chosen when it starts or under
  **✎**, from the list kept under **Settings → Clients** (or typed there and then as "New
  client…"). The client shows beside the case name, and the case list can be filtered by it.
  Renaming a client renames it on its cases; deleting one leaves its cases on disk but
  belonging to no client. The point of it is what the AI may be told about earlier cases: see
  **Distil runbook** below.
- **Context ▾**, next to the model, shows `Context 23k / 200k`: the prompt size of the last
  request (kept with the case, so a resumed case shows it too), against the selected model's
  context window when that is known. It turns amber past the threshold in **Settings →
  General** (default 100k) or at 75% of the window, whichever comes first, which is the cue to
  export a ticket summary and start a fresh case; red at 90% of the window means the next
  request may not fit. The window comes from the provider's model details (NanoGPT, Anthropic,
  OpenRouter), from the provider's Model overrides → Context window (set it for local models
  to match the server, such as Ollama's `num_ctx`), or from the error the first time a request
  is too long.
- When a request is too long for the model, the chat says so with **What the AI knows…** and
  **Retry**: compact or remove earlier exchanges, then retry, or pick a model with a larger
  window. Some local servers (Ollama) cut an over-long prompt silently instead of refusing it;
  set their context window in the provider's Model overrides so the figure warns in time.
- Terminal copy/paste: Ctrl+Shift+C / Ctrl+Shift+V. Control characters are left out of what
  you paste into a terminal (line breaks and tabs stay), and a notice says how many: an escape
  sequence in the clipboard would end the shell's bracketed paste and run the lines after it.
- A link that a session's output sets on its text (a terminal hyperlink) opens like a link in
  the chat: `http` and `https` only, in your browser, and always after a dialog that names the
  address in full, since the text on screen can say anything. Plain addresses in the output
  are not made into links.
- A terminal whose connection to DA Toolkit drops connects again by itself, with the session's
  recent output shown again.

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
lists the host's baselines, newest first, with the case each was taken in, its client and its
sensitivity. **Diff** gives a unified diff per section (services, ports, routes, disks,
packages, firewall, cron, users, …) that you can send to the AI; pick one taken while the
machine was healthy. Both snapshots are redacted before they are compared, so a section in
which only a secret changed is still listed, without the old or the new value ("differs only
in values that were redacted: a password, key or other secret changed"). **Delete** removes
a baseline, for example one saved by mistake on a broken machine. Baselines belong to the
host, not the case: deleting a case leaves them. They live under
`~/.local/share/datoolkit/baselines/<host>/`.

A baseline is filed under the host's name or address, which two clients' machines can share
(`192.168.1.1`), and a diff is made to be sent to the open case's model. So each baseline
keeps the client and the sensitivity of the case it was taken in (that case's as they are now,
while it exists), and a case can diff only against a baseline it may see: not one taken in a
case of a higher sensitivity, and not one taken for another client. Such a baseline is listed
with the reason and its **Diff** is off; it can still be deleted. One saved by an earlier
version whose case is gone says nothing of where it came from, and can be compared only in a
Sovereign case. The diff dialog names the case, client and sensitivity of the baseline it
shows.

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
  and each item is rehearsed once at a time. A rehearsal counts as read-only, so a line that
  chains, pipes or redirects (`;`, `&&`, `|`, `>`) gets one only if it keeps none of the line's
  own commands (a firewall backup, `crontab -l`, a service's dependents): the rest would run for
  real. Edit a rehearsal and it is judged like any other command.
- **Second opinion.** **2nd opinion** on any pending item (read-only ones too, when you want
  to know what a command does before running it), or from the disruptive confirmation, asks a
  reviewer model what it does, what could go wrong, whether it exposes sensitive data, and for
  a verdict (proceed / proceed with care / do not run). Set a
  different model under **Settings → Model** (it must be allowed by the case sensitivity).
  The reviewer is sent the command with its risk label, stated purpose and rollback (a
  password typed into it blanked, as for the chat model), what the local rules flagged on it,
  the case's name and notes, the session it is for (kind, target, OS and shell) and the
  hypothesis board; never the conversation or the proposer's reasoning. After an answer or an
  error the button reads **Ask again**.
  **Automatic review** (off / disruptive commands / everything flagged) asks it in the
  background as each command is queued, and the verdict shows on the item; click it for the
  full review. It only adds warnings: a "do not run" verdict asks for confirmation before a
  non-disruptive command runs, but a review never lowers a risk level or clears a flag.
  Automatic reviews run only on the chosen reviewer, never the chat model.
- **Paired probes.** When the AI gives two commands the same `group` (a capture on one side,
  a ping from the other), **Run group** types them into their sessions at the same moment and
  the results carry start times. Which commands share a group is the AI's choice, so Run group
  always shows first what it will type: each command with its number, session and target, and
  what Run would have asked about for it.
- **Watch.** **Watch** on a read-only item wraps it in a bounded loop (every N seconds, M
  samples). When you send the result, iterations identical to the previous one are dropped.

### Photos, replay, context

- **📷** (or paste an image) attaches a photo of a screen, LED panel or label to the next
  message. Photos are resized locally, stored in the case directory and sent to the model
  under the same sensitivity gate as text. What the chat box holds, a message not sent yet and
  its photos, stays with its case: opening another case shows that case's own, and it is all
  back when you open the first again (or reload the page).
- **Screenshot → chat** (or 🖥 by the chat box) attaches a picture of the active session:
  a terminal's screen drawn from its buffer with its colours, or the remote desktop of an
  RDP session. Use it when a login lands on an appliance menu (OPNsense, pfSense, Sophos)
  rather than a shell: the layout makes that obvious where plain text may not.
- **Vision.** The model picker marks models that read images (👁), text-only ones, and
  reasoning models (🧠), from the provider's model details (NanoGPT, Anthropic, OpenRouter;
  Private Mode models borrow their public twin's; others can be marked in the provider's Model
  overrides → Reads images), and routed models (⚡). It also
  marks **✓ tested** the models DA Toolkit's prompts were evaluated with (hover for the verdict,
  or type "tested" in the filter): see [which models work well](docs/model-evaluation.md). A
  variant of a tested model (a flash or thinking edition) is another model and isn't marked.
  With a
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
  The page shrinks what you attach to 2048 px on its long side. The server takes a picture of
  up to 4096 px a side and about 12.6 million pixels (a phone's full 12-megapixel photo), and
  refuses a larger or a damaged one ("Could not use an attached image: …"), going by the
  size the file states before any of it is decoded.
- **Exports are saved where you choose**: the app window opens the system Save dialog (in
  `--browser` mode the browser downloads the file). A copy still goes into the case folder.
  - **Export transcript (Markdown)** is the readable case record. Text that came from the web
    or from a model (a source's title and address, the AI's messages, a rollback or a note in
    the queue table) is written so that it can't act as markup of its own: an image or an
    HTML tag in it is shown as text, so a viewer that reads Markdown as CommonMark does
    (GitHub, VS Code's preview) fetches nothing when you open the file, and a source's title
    can't point its link elsewhere. Your own photos are still images.
  - **Full export** is a ZIP with every request DA Toolkit made to a model, in order: the
    system prompt (in full whenever it changed), the messages sent (as sent: for a model that
    can't see images, the vision helper's description where the image was; a note DA Toolkit
    put in the conversation itself is labelled "DA Toolkit → AI"), the model's reasoning as
    the provider returned it, and its reply with tool calls (`full-transcript.md`); the images
    exactly as the model received them; the AI-written documents; and the raw data (request
    log, conversation, chat, queue, hypotheses, audit log). Raw terminal transcripts are
    optional because they are unredacted. Cases started before request logging existed get
    their earlier conversation reconstructed, marked as such.
  - Ticket summary, client update and runbook have **Save as…** next to Copy. The model that
    writes them is told which messages were DA Toolkit's own notes and not your words, and,
    where exchanges were removed or compacted, that what it reads is not the whole case.
- **Context ▾ → Timeline replay** plays the case back: events on the left, the terminal
  transcript on the right, on one slider.
- **Context ▾ → What the AI knows** shows the exact context the model gets next turn, with a
  size estimate per exchange (a photo counts as 1,500 tokens, about what providers charge for
  one), and lets you remove exchanges from it. The chat and the audit log keep them.
- **Compact** (in the same dialog) has the chat model summarise the older exchanges, by
  default all but the last two, which stay word for word. The summary keeps the problem,
  findings with their evidence, every change made to a system, what was ruled out, your
  instructions and open threads, with hostnames, addresses and errors copied exactly. You
  see it and can edit it before **Apply**; nothing changes until then. **Cancel** while it is
  being written stops the request at the provider. The request uses the
  chat model, so the case's sensitivity rules apply as usual, and with Claude it reads the
  conversation from the prompt cache while that is warm. Applying changes the start of the
  conversation, so the next turn writes the shorter context to the cache once (see Prompt
  caching). After a request that was too long, only the part to summarise is sent, and a
  compaction that turns out too long itself is tried again that way. The
  previous context is saved in the case folder (`context-before-compact-N.json`), and
  **Undo last compaction** puts it back while the summary still opens the conversation;
  exchanges added since are kept.
- **Export ▾ → Client update** writes the plain-language version of the ticket note.
- **Export ▾ → Distil runbook** turns a solved case into `runbook.md`. A runbook reaches the
  AI in a later case only within one client: the new case must belong to a client and have
  **Include this client's history** ticked, and then, when its first message with words in it
  (a case may begin with a photo or with results alone) resembles one of
  that client's earlier cases of the same or a lower sensitivity (a Confidential case may use
  Open and Confidential ones, an Open case only Open ones), those cases are noted in the chat
  and their runbooks go into the AI's context as leads: text from an earlier case, which the
  AI is told not to take as instructions. A case with no client, or with the box
  unticked, never gets another case's runbook, and nothing from one client's cases reaches
  another client's. Changing the client or unticking the box drops them again. A case opened
  again keeps its runbooks (read afresh, and only those still of its client and sensitivity).
  **Context ▾ →
  Similar past cases…** is your own search across every case, on this machine only, and
  **Context ▾ → Open case folder** opens the case's files.

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
(`sudo ufw allow 48443/tcp`). It listens on one address, the one its codes carry: this
computer's address on the network its default route leaves by, so it is not also offered on a
client's VPN or a container bridge. Only where the computer has no such
address to name does it listen on every interface. The main window stays on 127.0.0.1 and is
never on the LAN.
A request without the phone's token is refused on its headers, before any of its body is read,
so nobody on the network can tie the app up by sending it data.

**Pairing takes two scans**, so that nothing secret crosses the network before you have
checked who the phone is talking to, and the computer allows each phone itself:

1. **Check the certificate.** Press **Start** and scan the first code. It opens a page with
   no secret on it. The browser warns that the connection isn't private: DA Toolkit uses a
   self-signed certificate made for this install. Accept the warning, open the certificate
   details (Android Chrome: the icon left of the address → Certificate information; iPhone
   Safari: Show Details → view the certificate → More Details) and compare its **SHA-256
   fingerprint** with the one in the dialog, every pair of it. If anything differs, press
   **It doesn't match: stop**: someone may be intercepting traffic on that network.
2. **Connect.** Press **Fingerprint matches** and scan the second code in the same browser.
   It carries a pairing code, good once and for five minutes. The browser has already
   accepted this exact certificate, so it must not warn again. If it does, don't continue:
   go back to step 1 and check the certificate.
3. **Allow it.** The phone shows a four-digit number, and the computer asks **Allow this
   phone?** with a number of its own. Allow it only if the two match: a code photographed and
   used from somewhere else shows another number. **Refuse** is the default. Allowed, the phone
   is given a token of its own, which only it has (DA Toolkit keeps only a hash of it).

**A phone that is already paired** stays paired while the companion runs. A phone pairing
again can skip straight to step 2 (**Already paired: skip**). Its browser accepted this
certificate before, so it connects without a warning. If it warns anyway, treat it like any
other warning and go back to step 1: browsers forget accepted certificates after a while, and a
new phone or someone in the middle produces the same warning. The browser shows that warning
before it sends the request, so refusing it keeps the pairing code on this machine.

The certificate is kept (`~/.config/datoolkit/companion-cert.pem`, key readable only by you),
so its fingerprint stays the same and each phone checks it once (until its browser forgets). **New certificate** makes a new one (every
phone checks again); a new one is also made when it expires, after about two years.

- The dialog lists the paired phones, each with **Disconnect**, which stops its token
  working. **New code** makes a new pairing code (the one shown before stops working) and
  leaves paired phones as they are; **Stop** disconnects all of them, and they pair again
  when the companion starts again. The button shows how many phone pages are open.
- The pairing code is only in the second code's `#` fragment, which the browser never sends in
  a request, and the page removes it from the address bar and history; the phone keeps its
  token for its tab only.
- Every answer on the companion's port says it may not be cached, framed by another page or
  sent on as a referrer.
- The companion's port serves only the phone's page and its API. **The phone receives** the
  case's name and sensitivity (not its notes, its client or its folder), the last 30 chat
  messages, the queue with its commands, the hypothesis board and the list of open sessions
  (each one's id, kind and target). It can mark queue items ran, skipped or pending (with a
  note), mark hypotheses, and send a photo with a description to the AI. Its "pending" undoes
  its own ran or skipped: a command the AI withdrew can be restored only at the computer. It
  cannot reach a terminal or run anything, open or close sessions,
  see settings, prompts or credential dialogs, or send results or plain messages. Whatever
  the AI proposes in reply still needs your click on the computer.
- The chat does go to the phone, so think twice before pairing on a Confidential case.

### Tool cache

**Settings → Tool cache** holds vetted portable CLI tools (WizTree, nmap, Sysinternals, …)
with a SHA-256 each. **Send to session** queues the transfer: `scp` from a local shell for
SSH hosts (with the host's key, jump host and options, as its session uses them), or an inline
PowerShell write (under 4 MB) for WinRM sessions, followed by the tool's run command. The hash
is checked on arrival and nothing runs without your click. For SSH the check is a read-only
item between the copy and the run, which compares the copy's SHA-256 with the one it must have
and says which: `sha256sum -c` prints `OK` or `FAILED`, and on a Windows host "OK: the SHA-256
matches" or "MISMATCH: the file is not the one in the tool cache; do not run it". The WinRM
command ends by printing `True` or `False` for the same comparison. The tool's file name is
passed as data to whichever shell reads it, so a space or a `$(…)` in it does nothing; a name
no command can carry safely (a control character; for a Windows host over SSH also `"`, `$`,
a backtick, `%` or `!`) is refused with a message to rename the file.

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
| Ctrl+Shift+1 … 9 | Switch session tab, from anywhere |
| Alt+1 … Alt+9 | Switch session tab, except inside a terminal, where Alt+digit goes to the program (tmux, irssi, the shell) |
| Ctrl+Shift+Enter | Run the next pending **read-only** command (never a modifying, disruptive or sensitive one, or one the reviewer said not to run) |
| Ctrl+Shift+K | Focus the chat box (showing the chat if it is hidden) |
| ↑ / ↓ in the chat box | Recall earlier messages |
| Enter / Shift+Enter | Send / newline |
| Ctrl+Shift+C / V | Copy / paste in the terminal |

Everything that acts on a click can be reached and pressed from the keyboard. The model list
takes the arrow keys, Home and End, with Enter or Space to choose (the down arrow leads into it
from the filter); session tabs, the `#N` chips, the sensitivity badge and a route's hosts take
Tab, then Enter or Space. A dialog holds the keyboard while it is open: Tab goes round inside
it, Escape closes it wherever the focus is, and the focus returns to what had it. A dialog you
have typed or changed something in (Settings, a case or host form, Send results) asks **Close
without saving?** before Escape or a click outside it discards that. A prompt that arrives by
itself while you are typing (a session asking for a password or a host key, a phone asking to
pair) does not take your keys into its field: it says so, and they go nowhere until you click
the field or press Tab.

## AI providers

Settings → AI providers holds as many providers as you like, each an address, an API key (in
the keyring) and the API it speaks. The model picker lists each one's models, and the top bar
always shows the provider the chat model is on, beside the model (hover it for the kind, the
API and the address). A provider's name can't contain `|`, which separates provider and model
where a setting names both. Deleting a provider removes its key and everything DA Toolkit had
learned or set for it (its models' details and context windows, its place as reviewer, vision
helper or research model, its recent models), so a provider added later under the same name
starts with nothing. When the keyring can't be used (it is locked, or its unlock prompt was
dismissed), saving, renaming or deleting a provider or a host is refused and nothing is
changed.

| Add… | Address | Speaks | What it brings |
|---|---|---|---|
| NanoGPT | `https://nano-gpt.com/api/v1` | OpenAI's API | Most models, `private/` (E2EE) and `TEE/` models, web search and the research agent, routes |
| Anthropic | `https://api.anthropic.com` | Anthropic's Messages API | Claude directly: its reasoning shown, prompt caching, Anthropic's own web search (optional), the refusal fallback |
| OpenRouter | `https://openrouter.ai/api/v1` | OpenAI's API | Many models, with their image support and context windows reported; routes by priority |
| WaveSpeed | `https://llm.wavespeed.ai/v1` | OpenAI's API | Open models |
| Local | `http://localhost:11434/v1` | OpenAI's API | Ollama, LM Studio, vLLM: the Local tier |
| Other OpenAI- or Anthropic-compatible | yours | either | Anything else that speaks one of the two |

**A redirect is never followed**, since it would send the conversation and the key again to
wherever it points: the error says where the provider redirected, so you can put its new
address in Settings → AI providers if it has moved. An address that answers with something
other than the API (a web page, a network's sign-in page, an address missing its `/v1`) is an
error naming the status and content type it answered with, never an empty reply. A server that
takes no connection (a local model server that is switched off) fails the request after ten
seconds; a reply that has begun may take minutes between its pieces.

**A proxy is used only beyond your own network.** DA Toolkit has no proxy setting of its own:
its requests go through the proxy your environment names (`HTTPS_PROXY`, `HTTP_PROXY`,
`ALL_PROXY`), if any. A provider on this machine or its local network (an address the Local
tier counts: see [Model tiers](#model-tiers-and-end-to-end-encryption)) is always reached
directly, for every request and its model list, so the key and a Sovereign case's
conversation never pass a proxy on their way to a model server of your own. Nothing else of the
environment rides along: the variables the OpenAI and Anthropic libraries read by themselves
(`OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_CUSTOM_HEADERS`, `ANTHROPIC_CUSTOM_HEADERS`) add
no header to a request, and a provider is sent its own key only.

The **API** is worked out from the address (Anthropic's for `api.anthropic.com`, OpenAI's for
everything else) unless you choose it. An Anthropic-compatible service gets the plain Messages
API request (messages, tools, system prompt, temperature) without Anthropic's own extras; if
it has no model list, the provider's default model is the one offered. A provider set to the
Anthropic API lists no `private/` models and can't be given one: those are only ever sent
sealed, which the OpenAI path does.

**On Anthropic's own API** the request uses what only it has:

- Claude thinks adaptively, at the reasoning effort in Settings → Model, and its reasoning
  summary is shown as it works. Temperature isn't sent: Anthropic's newer models don't take it.
- The conversation is cached (Prompt caching, Settings → Model), as through NanoGPT.
- The conversation is kept **append-only**, which Anthropic's newer models check: each reply's
  own content (its reasoning, with the signature that binds it to the conversation, search
  results, citations) is sent back unchanged, and the case's state (sessions, queue,
  hypotheses) goes after the conversation as a system message that applies to that request
  only and is sent again, word for word, every time after (it then costs nothing). After a
  compaction, a removed exchange or an edited case note, Claude's earlier reasoning is left
  out of what follows the change (it was written for a conversation that no longer exists),
  and the conversation carries on. A Claude model newer than this version of DA Toolkit knows
  is sent what the newest it knows need (append-only, adaptive thinking, no temperature), since
  each generation has been the stricter one.
- When one of Anthropic's safeguards declines a request (security work can trip the cyber
  classifier), Anthropic retries it on another Claude model where it can, and the AI's message
  says which model answered. When nothing could, the message says so, and nothing the declined
  reply began is queued.

**Model overrides** (in the provider's form) correct what DA Toolkit detects about a model, or
fill in what the provider doesn't report (local models, mostly): one row per model, with its
**tier**, whether it **reads images**, its **context window** (32768, 32k or 1M; match the
server's own setting, such as Ollama's `num_ctx`) and, on NanoGPT and OpenRouter, its **route**.
**Load the model list** fills the model boxes' suggestions and shows what is detected for each
column (on Auto). A provider's list that can't be read (NanoGPT's, OpenRouter's or Anthropic's,
which say which models see images, which efforts they take, their prices and windows) is asked
for again when a model is chosen or a message sent, half a minute after the last try at the
soonest; a list read once is kept. A tier can be set to Standard, TEE or Local, never E2EE:
only NanoGPT's
`private/` models are sealed. Marking a cloud model Local lets Confidential and Sovereign
cases use it, so do that only for a model on your own hardware.

**Routes: which host serves a model** (NanoGPT and OpenRouter). Most open models (GLM,
DeepSeek, Kimi, Qwen) run on several hosts, and the provider picks one. The route button in
the top bar (or **Route…** in a model's override row) asks for another:

- **Provider's own routing**: the default. On NanoGPT's subscription, usually the cheapest
  host at FP8 or better.
- **Fastest first word**, **Fastest overall** or **Cheapest**.
- **A specific host** (NanoGPT), from a table of each host's precision, privacy terms, NanoGPT's
  measured first token and speed, prices and caching. It is a preference, not a pin: if that
  host is down, another serves the call.

**FP8 or better**, ticked by default, leaves out hosts that run the model at lower precision
or don't say (SealedLore measured FP4 hosts slower and less accurate). A route belongs to the
model wherever it is used (chat, research, reviews, the vision helper) and never applies to
`private/` or `TEE/` models, whose enclave is their host. **Any NanoGPT route but its own is
billed pay-as-you-go, even for a model the subscription includes.** NanoGPT's replies don't
name their host, so each routed reply's bill is the evidence: the AI's message shows **⚡
routed** when it was billed as routed (at the chosen host's price, for a host), and **⚠ route
not followed** when NanoGPT billed nothing (it served the call on its own routing, as it does
without a word when the host is down) or at a clearly different price (another host served
it). Each host's privacy terms are its own; NanoGPT's account settings can require hosts that
keep nothing, for every route.

## Generation settings

Settings → Model sets what goes with every request: temperature (default 0.3), reasoning
effort (default medium), max output tokens, top-p, frequency and presence penalty, and seed.
Blank means the model's own default. Reasoning effort is sent only to reasoning models and is
moved to the nearest level each model accepts (Kimi and GLM take low / high / max; Opus low to
max), the cheaper of two as near, but never to none, which would turn reasoning off. The same
settings apply through NanoGPT Private Mode, where every `private/` model is held to the levels
every private build takes (low, high, max), whatever its `TEE/` twin's listing says: private
DeepSeek V4.1 Flash refuses medium. A model that refuses the level it was sent and names the
ones it takes is asked once more with the nearest of those, and sent it from then on; a note
in the chat says so. On Anthropic's API, effort goes as
Anthropic's own (none and minimal become low: its newer models always think), and temperature
and top-p go only to models that take them; frequency and presence penalty and seed aren't
Anthropic settings.

**Prompt caching** (Settings → Model, default on for 1 hour): Claude models through NanoGPT
or Anthropic (Open cases) reuse the conversation each request already sent, so later requests in a case
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

What each tier lets a provider see, and everything else DA Toolkit sends and keeps, is also set
out in [Security and privacy](docs/security-and-privacy.md).

| Tier | Detected from | What the provider can see |
|---|---|---|
| **Standard** | anything else | Everything |
| **TEE** | ids with `TEE` as one of their `/` parts (`TEE/…`, `…/tee/…`; case doesn't matter), ids starting `phala/`, and ids ending `-tee` or `:tee` | Attested before anything is sent (for most `TEE/` models, RedPill's gateway, which passes each request on to the machine that runs the model), but prompts pass NanoGPT's gateway **in the clear** |
| **E2EE** | NanoGPT `private/…` ids, however written (`Private/…` too) | Ciphertext only, plus your account, the model, timing, sizes and usage |
| **Local** | a base URL on this machine or its local network (see **Local trusts your network** below), whatever the model is called | Nothing leaves your network |

The first of these that applies is the tier: one you set for the model in the provider's Model
overrides, then Local for any model at a local address, then E2EE for a `private/` id, then
TEE, and Standard for the rest.

**Requests go over https, or not at all.** A base URL that is plain `http://` is accepted
only for this machine and its local network, which is how a model server of your own is
reached:

- `localhost`, and names ending `.localhost` or `.local`;
- `127.x` and `::1` (this machine);
- `10.x`, `172.16`–`31.x` and `192.168.x`;
- link-local addresses (`169.254.x`, `fe80::…`) and IPv6 unique local ones (`fc00::/7`, such
  as `fd00::5`).

A name is judged as written and never looked up to see where it points. A `.lan` name is not
on the list: it counts as Local (below) but is reached over https only. For any other address,
Settings → AI providers will not save it, and a
`config.toml` edited by hand to hold one sends nothing (chat, model list, attestation, web
search and research alike): the app says why instead. The address is never rewritten to
https for you, and certificates are always checked. An address is judged whole:
`http://127.0.0.1%2e.example.com/` is a name under example.com, not this machine, and is
refused; so is one with a space, a backslash or a control character in it.

A user name and password written into an address (`https://user:pass@host/v1`, a model server
behind one) are kept in the keyring, as the API key is: `config.toml` has the address without
them, and no message, notice or log shows them (the address is shown as
`https://…@host/v1`). The page itself is given the address whole, since the provider's form
in Settings shows it for you to edit; the provider list and the top bar show it without. They
are put back only while the file's address is that same one, so
editing the file to another server doesn't hand it the password; taking the password out of
the address in Settings takes it out of the keyring. A password found in a `config.toml` that
was edited by hand, or written by an earlier version, is moved to the keyring and out of the
file at the next start; where no keyring can take it, it stays in the file and is used from
there, so on such a computer the file can hold that one secret. An API key with a line break,
a space or
another character no API key has (picked up where it was copied from) is refused when saved,
and one stored before is refused before anything is sent with it, saying so without quoting it.

**Local trusts your network.** A base URL counts as Local when its host is `localhost`, an
IP address plain http is accepted for (this machine's, a private, link-local or unique local
one: the list above), or a name ending `.local` or `.lan`, so Confidential and Sovereign
cases may use it. With `http://` to a private IP or a `.local` name, prompts (client data
included) and the API key cross the LAN unencrypted, and such a name is only as trustworthy
as your DNS. Use Local over the network only on a LAN you trust, or put the model server
behind HTTPS (a `.lan` name is reached over https only). A Local provider is never reached
through a proxy from your environment (see [AI providers](#ai-providers)).

Override a model's tier (Standard, TEE or Local; never E2EE) under **Settings → AI providers →
the provider → Model overrides**.

**Web search and the research agent are gated by approval, not by tier.** Search queries reach
the search provider in the clear whatever the chat model's tier. The research model works
from the brief alone (never the case), the same kind of text as a search query, so it may be a
Standard-tier model in a Confidential case: there, every brief waits for your approval and
edit. Sovereign cases do neither.

**How E2EE works** (`src/datoolkit/llm/private_mode.py`, following SealedLore's implementation):

1. **Attest.** Before anything is sent (even NanoGPT's billing check), the enclave is attested
   with Tinfoil's own verifier, pinned as `tinfoil==0.15.0`. Its fetches, and those of the
   Sigstore library it checks the release with (Sigstore's trust root among them), follow a
   redirect only to https, never from the internet into this computer or its network, and for
   the trust root five at most. One they may not follow fails the attestation, and nothing is
   sent; a version of that software DA Toolkit can't hold to this is refused, saying why, rather
   than used without it.
   - The hardware report (AMD SEV-SNP or Intel TDX) must match the router release Tinfoil
     published, as recorded by Sigstore.
   - The report must bind the key that requests are sealed to.
   - A release older than the latest on Tinfoil's GitHub is flagged, not refused.
2. **Seal.** The whole request body is sealed on this machine to the attested key (EHBP: HPKE
   X25519 + AES-GCM), including the conversation, the tool definitions and a private
   prompt-cache secret. The reply comes back sealed.
3. **Refuse** anything that doesn't fit:
   - a reply that isn't sealed or isn't marked Private Mode, whatever its HTTP status (NanoGPT's
     relay can't read what it carries, but it could answer a request itself, with a redirect);
   - a stream that ends early. An error the enclave itself sends partway through a reply (the
     conversation too long for the model, say) is shown as its own ("the model stopped: …"),
     so a context overflow is explained as one, with the way to compact;
   - a failed attestation. Nothing is sent, and a `private/` model never falls back to the
     plain endpoint.

   What the relay writes in an error is never sealed. So the error says it is the relay's
   ("NanoGPT's relay said (unauthenticated): …") and shows it on one line, short and without
   invisible characters. Whatever of the relay's a refused billing check or a failed
   attestation quotes is shown the same way, because an error can reach the AI: a research
   run that fails reports its error to it.
4. **Show and log it.**
   - The top bar shows **🔐 attested**; hover for the enclave, release and key fingerprint,
     and click to re-check. A re-check attests the enclave again there and then, whatever
     attestation is kept; if it fails, none is left in force and nothing is sealed to the
     enclave until one holds.
   - Replies are marked "end-to-end encrypted".
   - Each attestation is recorded in the case's `events.jsonl`.
   - Attestation is redone every five minutes (time the computer spends asleep counts).

NanoGPT retires Private Mode models from time to time, and the enclave (Tinfoil's router,
whatever the model) would still attest. So before a `private/` model is attested or sent to,
it is looked up in NanoGPT's public list of them (no key is sent; the list is kept five
minutes, like the attestation). If it is gone, nothing is sent: the top bar reads **⚠ no
longer offered** (hover for the models still listed), the chat note after a message offers
the end-to-end encrypted models the list still has, and **Ask again with it** sends your
message again with the one you pick, which the conversation then carries on with. A vision
helper or research model that is gone says so the same way. A list that cannot be read says
nothing either way: NanoGPT's own check still refuses a model that is gone, and the note then
says why.

Like SealedLore, DA Toolkit doesn't use NanoGPT's `npx @nanogpt/private-mode@latest` proxy,
because that would let NanoGPT change the code that holds your plaintext.

Verifying an enclave contacts NanoGPT's relay (for the attestation bundle), Tinfoil's mirrors
of GitHub (`github-proxy.tinfoil.sh`: the router's latest release and its Sigstore bundle) and
of AMD's and Intel's certificate services (`kds-proxy.tinfoil.sh`, `tdx-proxy.tinfoil.sh`), and
Sigstore's public trust root (`tuf-repo-cdn.sigstore.dev`). Nothing of the case goes to any of
them, and no key to Tinfoil's mirrors or Sigstore.

**How TEE attestation works** (`src/datoolkit/llm/tee.py`, `dcap.py`, `nras.py`, `ethsig.py`,
ported from SealedLore with the checks unchanged, `dcap.py` and `nras.py` kept alike in every
app of the author's that attests TEE models, and tested against a real attestation captured from
`TEE/glm-5.3-flash`):

1. **Attest before sending.** NanoGPT's `/tee/attestation` is asked for evidence bound to a
   fresh random nonce. Nothing goes to a TEE model (chat, vision helper, write-ups, the
   second-opinion reviewer, compaction or the research agent) until that holds, and it is made
   again before a send once it is 15 minutes old (time the computer spends asleep counts), or
   once one of the model's replies has failed its check (step 4). That is looked at before
   every request, not only a turn's first: each further round of a turn and each request of a
   research run waits for it too, so a turn that outlasts its attestation, or a re-check
   refused while it runs, stops there. Nothing is sent while the
   model is being attested either (it was just chosen, or chosen again, or you clicked to
   re-check, also while a request was already waiting): a request made then waits for what
   that attestation finds. One that is refused
   leaves no earlier attestation in force, so nothing more goes to the model until one holds.
   An attestation is of the address it was made at: change a provider's address in Settings,
   delete the provider or pick another model, and the top bar shows none until the model in
   use is attested as it is now. A new API key saved for the provider has its TEE models
   attested again before the next request, since each reply's record is looked up with the key.
   - The Intel TDX quote's own bytes must bind the enclave's reply-signing key and our nonce
     (never NanoGPT's JSON copy of them), or bind RedPill's attested keys, that key among
     them, and our nonce. For per-instance providers (Chutes), each instance's key, bound
     into its quote, must have signed our nonce and that very quote, and every instance must
     attest.
   - Beside it, with a nonce of its own, NanoGPT is asked for RedPill's gateway's own report
     (since November 10 2026), whose quote binds the gateway's whole keyset to our nonce: its
     receipt key and the keys that sign each reply's record. A model whose signing key is in
     that keyset has its receipt keys bound by the hardware too. A model whose own evidence
     can't be had for our nonce, but which the gateway answers (`TEE/deepseek-v3.2`, whose
     evidence is relayed for the relay's nonce, and `TEE/qwen3.6-27b`, whose attestation times
     out), is attested through the gateway instead, and each of its replies is verified only
     with a receipt that holds (step 4). A report that doesn't bind our nonce is refused;
     where NanoGPT has none (models only Chutes or Tinfoil serves), the model's own evidence
     is taken as before. An attestation the provider took too long for is asked once more
     with a new nonce; each request may take up to a minute and a half (Chutes' evidence
     covers every instance). One attestation as a whole, Intel's and NVIDIA's checks included,
     is given three minutes; past that it is refused ("wasn't done within 3 minutes, and was
     given up") and can be tried again.
   - The quote is verified up to Intel's SGX root, which is pinned in the code: the PCK chain
     (every certificate that signed another a certificate authority allowed to), Intel's own
     TDX quoting enclave (checked on every quote, whether or not Intel's service can be
     reached), the quote's signature, Intel's revocation lists, and Intel's TCB information,
     which counts only when signed by Intel's TCB signing certificate. A debug TD, a TCB Intel
     revoked (the platform's, the TDX module's or the quoting enclave's) or never listed, or a
     forged chain is refused.
   - The GPU evidence goes to NVIDIA's attestation service. Its ES384-signed verdict must be
     for our nonce, current, and pass for every GPU (measurements, secure boot, debug off).
     If NVIDIA's signing keys can't be fetched, the verdict is still held to all of that and
     only its signature goes unchecked; keys fetched before are used when fresh ones can't be.
2. **Refuse** evidence relayed for a nonce the relay chose (replayable) when the gateway can't
   stand in, no attestation at all (Tinfoil-hosted `TEE/` models; use their `private/` twin),
   attested keys past their expiry, evidence in no shape to be read (refused as that, "…
   couldn't be read"), or anything that fails the checks above. Nothing is sent. Whatever a
   refusal quotes of the relay or the provider is shown as Private Mode's relay errors are: on
   one line, cut short and without invisible characters, since a refusal is shown in the top
   bar, logged and, when a research run fails on it, passed to the AI.
3. **Partial** when nothing failed but something couldn't be checked, isn't current, or falls
   short: Intel's collateral or NVIDIA's service or keys out of reach, no GPU evidence
   offered, **GPUs not tied to the quote** (NanoGPT's report carries GPU evidence made for our
   nonce, which NVIDIA vouches for, but the attested machine's quote doesn't commit to it, so
   the GPUs may be another machine's, the one running the model, say; a Chutes instance's
   quote does bind its GPUs, so theirs aren't named so), a TCB status other than UpToDate,
   the receipt keys not in the quote (where NanoGPT
   has no gateway report for the model: its quote binds the key that signs each reply's
   record, and RedPill's receipt keys are only published beside it), and RedPill's gateway,
   which passes each request on to the model's host, so that the model itself runs in an
   enclave isn't shown by its quote. A model attested through the gateway alone has no GPU
   evidence either: its own enclave is then the gateway's word, in each reply's receipt. A
   model Chutes answers first that the gateway answers now and then (GLM 5.1 and 5.2, Kimi
   K2.6) is partial the same way, its instances attested and the gateway too; where NanoGPT
   couldn't give the gateway's report just then (a rate limit, an outage), it falls short of
   **RedPill's gateway unchecked** instead. The
   top bar shows **🛡 TEE gateway attested (partial)** where RedPill's gateway is what was
   attested, else **🛡 TEE attested (partial)**; the tooltip leads with what was attested and
   what that leaves to someone's word, then names each shortfall and the keys its replies are
   checked against. Sending still goes ahead.

   **What is attested** is said in the top bar, its tooltip and the notice after a re-check:
   - For every model RedPill serves (most of NanoGPT's `TEE/` models), the attested machine is
     **RedPill's TEE gateway**, and the top bar says "TEE gateway attested". The gateway passes
     each request on to the machine that runs the model (in November 2026 Phala's, Fortanix's,
     NEAR AI's, 0G's, Tinfoil's router or Chutes'). That machine isn't attested by DA Toolkit:
     that it is a verified TEE is the gateway's word, in each reply's signed receipt.
   - For a model Chutes serves itself, **each instance** Chutes lists, each with its own GPUs.
     Chutes signs no replies, so nothing shows which machine answered: NanoGPT routes each
     request, and a model may be served elsewhere when its provider is busy.
   - Otherwise the one enclave whose key signs the model's replies; whether that machine runs
     the model itself or passes requests on isn't shown.
4. **Check each reply,** wherever it comes from: the chat's, an image's description by the
   vision helper, a write-up, a second opinion, a compaction summary, and each of the research
   agent's (its page checks too). After each reply DA Toolkit fetches the record NanoGPT keeps
   for it (by the receipt id the reply named, waiting a little while it is written, or by its
   id), signed with EIP-191 by the attested key or, through RedPill's gateway, by any key its
   quote binds. The record names a hash of the reply as the gateway sent it, which for most
   models is the exact stream DA Toolkit received, so a TEE model is always asked for a stream,
   a write-up or a second opinion too; where RedPill's signed receipt comes with it, that is
   checked too (Ed25519, under the keys kept at attestation, for the same request and reply,
   its upstream verified). Each reply is marked
   with what its check showed, beside the AI's message, the image's description, the write-up,
   the second opinion in the queue, the research card or the compaction summary; a turn or a
   research run takes the mark of its worst reply:
   - **✔ verified**: signed by the attested key, for the exact bytes received, its receipt
     (where read) holding. For a model RedPill serves, the receipt says the machine the reply
     came from was verified: that last part is the gateway's word. A model attested itself is
     verified by its own key's signature, with a receipt or without, so the tooltip says
     whether one was read: without one, nothing says the machine running the model was
     verified;
   - **signed, text not compared**: signed by the attested key, but NanoGPT rebuilds this
     model's replies, so the text can't be compared with the record;
   - **⚠ reply not verified**: no record of the reply could be read (the provider keeps none,
     it was out of reach, the gateway's keys changed since the attestation, or the reply named
     neither an id nor a receipt to look its record up by, or was cut short by Stop or a
     failed stream), or, for a model
     attested through RedPill's gateway alone, its record came with no receipt that holds: the
     gateway's signature says only that the reply passed through it, and the receipt is its
     word that the model's own enclave answered;
   - **✖ check failed**: the reply doesn't match its record: another key signed it, it names
     other bytes, or its receipt doesn't hold;
   - **unsigned**: the provider signs no replies (Chutes); every instance that could answer
     was attested before sending instead, but nothing shows which one answered. A reply
     RedPill's gateway answers in Chutes' place names its receipt, and is checked like any
     other.

   Each mark's tooltip says what was attested before the reply was sent and what is someone's
   word, and that the text passed NanoGPT in the clear.

   A failed check is also said in a notice, and the model is attested again before anything
   more is sent to it; a request made while a reply of the same model is still being checked
   waits for that check. Where a model was attested again between two requests of a turn, each
   reply is checked against the attestation its own request went out under. A reply whose
   check was still running when DA Toolkit closed reads "⚠ reply not verified" when its case is
   opened again. A research report with a failed check isn't kept for later cases.

What this proves: a genuine, current, non-debug Intel TDX machine (and, where evidence is
offered, NVIDIA GPUs) holds the keys, live, bound to our nonce, and each reply's record was
signed by one of them, for the exact text received where NanoGPT passes the stream through. What it
doesn't prove: which software the enclave runs (its measurements aren't compared with published
ones), what was sent (the record hashes the request as NanoGPT forwarded it, which DA
Toolkit can't reproduce), or, for a model RedPill serves, the machine that runs the model,
which is the gateway's word. The prompt also passes NanoGPT's gateway in the clear, so TEE models
stay out of Confidential cases; for that, use a `private/` model. Verifying contacts NanoGPT,
Intel (`api.trustedservices.intel.com`, `certificates.trustedservices.intel.com`) and NVIDIA
(`nras.attestation.nvidia.com`). Intel's collateral and NVIDIA's keys are cached for an hour
(time asleep counts); Intel's only once all of it has been read and its signatures checked, so
an answer that wasn't Intel's (a proxy's or a portal's page) is asked for again the next time.
With **Name the provider behind each TEE reply** on (below, off by
default), `api.redpill.ai` too.

**Name the provider behind each TEE reply** (Settings → Model → TEE models, off by default)
says who ran the model beside a reply's mark: "🛡 TEE · ✔ verified · run by Phala", say. Each
receipt names the gateway's session with the machine that answered, and RedPill publishes that
session's record, saying which provider it is (Phala, Fortanix, NEAR AI, 0G, Tinfoil's router
and others; the same model can be run by different ones from one reply to the next). With the
option on, DA Toolkit fetches that record after each reply whose receipt holds, and believes it
only if it is exactly the record the receipt names and the reply was served within the
session's time. This connects to `api.redpill.ai` directly, not through NanoGPT, so RedPill sees
your computer's internet address and that one of its models was used around then; only the
session's id is sent, never your messages or your key. The request is made as every attestation
fetch is: https only, no redirect followed, and through a proxy if your environment sets one. A
session's record is read once while DA Toolkit runs (a session serves a provider for about an
hour), at most a megabyte. It is never fetched for a model Chutes serves: each Chutes instance
has a session of its own, about 3.4 MB for `TEE/deepseek-v3.2`, and replies are spread across
them, so it would be megabytes nearly every message. What the record says is RedPill's word:
DA Toolkit doesn't check the machine it names, and the tooltip says so. A record that can't be
read or believed names nobody and says why; the reply's mark never changes. A turn, a research
run or a write-up names a provider only when all its replies name the same one. Each record
fetched is in the case's audit log and timeline: the host, the session's id and what came back.

## SSH, WinRM, RDP and windows

- **SSH** uses your system `ssh`, so `~/.ssh/config`, keys, the agent, jump hosts and
  known_hosts all work.
  - Password and host-key prompts appear as dialogs. A saved password is supplied once per
    session; if it's rejected, you're asked. A prompt is taken off the screen at once when the
    program that asked has gone (ssh gave up, or its session was closed).
  - **A saved password goes only to the host that is asking for it.** It answers a password
    prompt (never a key's passphrase, a PIN or a one-time code), and only one that names the
    session's own host in text no server can write: OpenSSH's own `user@host's password:`, or
    the `(user@host)` it puts before a prompt a server worded. A jump host on the way never
    gets the target's password, whatever its prompt says, and what you type for a jump host
    isn't offered for saving as the target's. Behind a jump host (the host's **Jump host**
    field), a prompt that names no host is shown to you and not answered from the keyring;
    and where this computer's OpenSSH is older than 8.4, which doesn't say which server worded
    a prompt, no prompt behind a jump host is. With so old an OpenSSH, put a jump host in that
    field and not only in `~/.ssh/config`: DA Toolkit can't see one that is only there, and
    would take its bare `Password:` for the target's.
  - Old network gear may need extra options, e.g. `KexAlgorithms=+diffie-hellman-group14-sha1`.
- **WinRM** opens a PowerShell remoting console (pypsrp) supporting NTLM, Kerberos, Negotiate
  and Basic, on 5985 or 5986.
  - **Kerberos** (and Negotiate, which tries Kerberos first and falls back to NTLM) needs:
    the user as `user@REALM` (the realm is the domain in capitals; `user@corp.example.com` is
    taken as `user@CORP.EXAMPLE.COM`), the computer by its full DNS name as the domain knows it
    (not an IP address), and this computer able to find a domain controller: through the
    domain's DNS (on its network, or over the VPN), or named in `/etc/krb5.conf`. Since it
    needs a name, plain HTTP (5985) works only for `.local` domains (see below); elsewhere use
    HTTPS (5986). The AppImage brings the Python side; MIT Kerberos' `libgssapi_krb5` comes from
    the system, where OpenSSH already put it. Run from source, DA Toolkit needs its `kerberos`
    extra for this: `.venv/bin/pip install -e ".[kerberos]"`, which compiles the gssapi and
    krb5 packages (neither publishes a Linux wheel), so install MIT Kerberos' headers and a C
    compiler with Python's headers first (`sudo apt install libkrb5-dev gcc python3-dev`; the
    Kerberos headers are `krb5-devel` on Fedora and part of `krb5` on Arch). A failure says
    which of these it was.
    Negotiate with `DOMAIN\user`, or with an address, uses NTLM.
  - Commands are single-line. `Read-Host` and `Get-Credential` prompts and tab completion are
    not supported.
  - Variables persist between commands.
  - Output appears as the command produces it, so a slow command or a watch shows its
    progress. **Ctrl+C** stops the running command within about a second and keeps what it
    printed so far; the session stays connected.
  - **HTTPS, or the local network.** Plain HTTP (5985) is accepted only for this machine and
    its local network (localhost, 10.x, 172.16-31.x, 192.168.x, `.local`), as for model
    providers; a host further away must use HTTPS. Basic authentication over plain HTTP sends
    the password in the clear, so it also needs **Allow Basic over plain HTTP** ticked on the
    host; NTLM and Kerberos encrypt the exchange themselves. Both rules are checked when the
    host is saved and again when a session opens. A host on this machine or its local network
    is always connected to directly, never through a proxy from your environment
    (`HTTP_PROXY` and the like), which would otherwise be handed that password; a host
    further away goes through the proxy, if one is set.
  - **The certificate is always checked.** Over HTTPS it is verified against the system's CA
    store, or, with **Validate certificate** unticked (a self-signed listener), by DA Toolkit
    itself: on first connection you are shown its fingerprints to verify, and it is pinned; a
    different certificate then blocks the connection until you forget the pin under Settings →
    Hosts, as for RDP. Windows' own tools show a certificate's SHA-1 thumbprint, so that is
    shown too (on Windows, `winrm enumerate winrm/config/listener` shows the listener's); the
    pin itself is the SHA-256.
- **RDP** opens the remote desktop in a tab, through Apache Guacamole's `guacd` on
  127.0.0.1:4822 (see the install table in the README: `sudo apt install guacd` on Ubuntu
  24.04 and Mint 22, and upstream's container on Ubuntu 26.04, Debian, Arch and CachyOS,
  which don't package it). If guacd isn't there,
  opening an RDP host shows the commands for your distribution in a dialog you can copy them
  from. DA Toolkit only connects to guacd on this machine, since that link isn't encrypted,
  and does the connection handshake itself, so the password never reaches the page. And only
  to a guacd that is yours or the system's: what listens on port 4822 must belong to you
  (guacd run by hand, or rootless podman's port forwarder) or to a system account (root's, as
  Docker's is, or the guacd package's service user). One another user of the computer started
  is refused, saying whose it is, and is sent nothing. Add RDP
  hosts under Settings → Hosts with the security mode and keyboard layout. Extended NLA isn't
  offered: every guacd available is built on FreeRDP 2, which can't speak it, and Windows
  accepts plain NLA on the same servers. A host saved with it by an earlier version says so
  when opened; choose NLA.
  - **The case log records whether the desktop really connected.** guacd takes the connection
    before it tries the remote desktop, so a wrong password or a security mode the server
    refuses is logged as a failed connection with guacd's reason (and shown in the case
    timeline), not as connected.
  - **The container runs only while you need it.** Where guacd comes from upstream's
    container, DA Toolkit starts it (as `datoolkit-guacd`, with podman, or Docker if that's
    what you have) when you open an RDP host, and stops and removes it when the last RDP
    session closes or DA Toolkit quits. You download the image once (`podman pull`, which the
    dialog shows); DA Toolkit never downloads it itself, and uses exactly the pinned 1.6.0
    image. The container is run with `--pull=never`, with no capabilities and unable to gain
    privileges: guacd reads what RDP servers send it and needs neither. Two DA Toolkits
    running at once share the one container, and it is removed when the last of them to use it
    closes its last RDP session or quits. A guacd that's already running, such as the
    distribution's service, is used as is and left alone. If you set up the always-running
    `guacd` container that earlier versions
    suggested, remove it so DA Toolkit can manage its own: `podman rm -f guacd`, and
    `systemctl --user disable podman-restart.service` if nothing else uses it.
  - **Certificates are trusted on first use and pinned.** Before every connection DA Toolkit
    reads the server's TLS certificate itself. The first time it shows you the SHA-256
    fingerprint and the SHA-1 thumbprint Windows itself shows, and under them the subject and
    issuer, to verify (on
    Windows: `Get-ChildItem 'Cert:\LocalMachine\Remote Desktop'` in PowerShell, or the Remote
    Desktop certificate in `certlm.msc`; `.GetCertHashString('SHA256')` on it gives the
    SHA-256); after that, a different certificate blocks the connection until you forget the
    pin in Settings → Hosts. The fingerprints stand first because the subject and issuer are
    text the server chooses: each is shown on one line, cut to 200 characters, so nothing in a
    certificate can draw a fingerprint line of its own (the same in WinRM's prompt). Servers
    offering only legacy RDP security have no
    certificate, so you are warned on every connection. A server whose certificate is pinned
    and which now offers no TLS at all is refused like one whose certificate changed, unless
    you set the host to legacy RDP security yourself: the answer that declines TLS is
    unencrypted, and anyone on the way can write it. With guacd 1.6 or later (the
    container) the pin is also handed to guacd, which checks the certificate on the
    connection itself; guacd 1.3 (Ubuntu's and Mint's package) cannot, so there the check
    and guacd's connection are separate TLS sessions, and DA Toolkit reads the certificate
    again before each connection the tab makes (Reconnect, a fit-to-window reconnect, a page
    reload) and holds it to the one the session was opened with; another certificate, or
    none, ends that connection with the reason in the tab. The **Negotiate (any)** security
    mode
    connects with what the server chose when its certificate was read (NLA or TLS), so a
    downgrade to legacy RDP security, which has no certificate to check, is refused.
  - **The pin file.** Pins are kept in `rdp_pins.json`. If that file can't be read (cut short,
    emptied, edited into something else), it is never taken for "no pins": it is set aside as
    `rdp_pins.json.unreadable-<date>-<time>`, the connection being opened is refused with a
    message naming both files, and from then on every RDP and WinRM host asks you to check its
    certificate again, as on a first connection.
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
    paste it, use "Copied text", or attach a screenshot. Typing there is key presses, which
    sets three rules. A command with a tab or another control character in it is refused
    before anything is typed (a tab would be the Tab key, which moves the focus or completes a
    name): replace the tab with spaces, or put the text on the remote clipboard with **Paste
    text…**, whose **Type it** follows the same rule. Every line break is the Enter key, so
    **Insert** of a command of several lines would run all but its last line, and asks every
    time before it does. And while a command is being typed, your own key presses in that
    desktop are held back, so none lands in the middle of it.
  - Limits: no smart-card or USB redirection, text-only clipboard, one monitor.
- **Window sessions** work through a window on your own screen: typically a ScreenConnect
  control window, but TeamViewer, AnyDesk, a VM console or an iDRAC/iLO console work the
  same way. Nothing needs setting up on the remote side, and DA Toolkit needs no login or API
  for the tool.
  - **+ Session → Window on this screen…** lists the open windows, remote-support tools
    first (a browser tab counts only when it's a console, such as iDRAC or iLO, not the
    tool's website). Pick one and give it a name (suggested from the title, e.g. the
    computer's name).
    The window is followed by its X window id, so moving or resizing it doesn't matter, and
    is known by the process and class it had when you picked it as well: the X server hands a
    closed window's id to a later window, and a session whose id is another program's window
    by then says its window has closed instead of showing that one. What the AI is told of
    the session is the window's class and title on one line, cleaned and cut short (a title
    is set by whoever is at the other end); the tab and the picker show the title as it is.
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
  - While a command of the session waits for its result, **Paste result → AI** and
    **Screenshot → chat** open **Send results** with it filled in (the text redacted as
    usual, or the screenshot attached and noted), for you to check and send. That way the
    command is answered and leaves the queue. With nothing waiting, they send the text on
    its own or attach the screenshot to your next message. RDP's **Clipboard → AI** and
    screenshot work the same way.
  - Commands that would cut the session are flagged: stopping or uninstalling the
    remote-support agent (ScreenConnect, TeamViewer, AnyDesk, ...), logging off, and the
    usual network changes.
  - **X11 windows only, including under XWayland.** Wayland doesn't let one program read
    another's windows, but programs that run through XWayland (the ScreenConnect client
    among them) are listed and captured as on X11. Native Wayland windows aren't listed.
    Tested on KDE Plasma 6 (Wayland, CachyOS): capture, following the window when it's moved
    or resized, and the clipboard both ways between DA Toolkit and the XWayland window.
  - A minimised window can't be captured: bring it back first. On X11 the same goes for a
    window on another workspace; under KDE's XWayland it can still be captured. Without a
    compositor (most X11 desktops have one), the parts of the window that other windows cover
    can't be read either; under XWayland they can.
- **Saved passwords** (SSH, WinRM and RDP hosts).
  - **A password is saved once the host has accepted it.** With **Save to keyring for this
    host** ticked in a password prompt, what you type is held in memory and goes to the
    keyring when the login has succeeded: for SSH when the remote shell starts, for WinRM
    when the console has authenticated, for RDP when the remote desktop is there. One the
    host refuses, or whose session ends first, is not saved, so a mistyped password is never
    kept. An SSH session that gets no terminal (`RequestTTY=no`) gives no such sign, and its
    password is not saved.
  - **A saved password that is refused is said to be that.** A WinRM or RDP host's saved
    password is used without asking. When the server refuses it, the console (or, for RDP, a
    notice beside guacd's error) says "The password saved for ‹host› was used, and the server
    refused it. To replace it: Settings → Hosts → ‹host› → Forget stored password, then open
    the session again and type the new one." An RDP security mode the server refuses is not
    taken for a refused password. SSH asks you itself when a saved password is rejected.
- **Linked sessions.** Sessions that reach the same machine are linked automatically as one
  (a coloured bar on their tabs): the same address by the same jump hosts, if any. The address
  alone doesn't say that: `192.168.1.1` behind one client's jump host and behind another's
  are two routers, and a loopback address (`localhost:3390`) is whatever was forwarded to that
  port, so there the port counts too, and a saved host at a loopback address is never linked
  to your local shell. Local shells link to each other. 🔗 links sessions by hand (hostname vs
  IP, NAT) or unlinks one for good. The AI is told which linked session takes commands and
  which is your desktop view (an RDP or window session), so it sends commands to the shell
  while you watch the GUI.
- **A session's environment is your own.** Run from the AppImage, DA Toolkit leaves out of the
  shells and programs it starts for you what the AppImage set for itself: its own variables
  (`APPIMAGE`, `APPDIR` and the like) and the `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` it
  points at the system's CA store for its own connections. A tool run in a DA Toolkit terminal
  so behaves as it does in any other, and a CA bundle you set in those variables yourself is
  kept.
- **When a session ends.** A terminal session ends when its shell or `ssh` exits, even if a
  background job started in it still holds the terminal (the job is left running). If a
  session's transcript can't be written (a full disk, usually), the terminal goes on working,
  since it may be the shell that frees the space: a line in the terminal says that the
  transcript is switched off for that session, and output from then on is not in the case's
  transcript, so copy what the AI should see from the terminal.

## Where things are stored

| What | Where |
|---|---|
| Config (providers, hosts, clients, settings; no secrets, but for a password in a provider's address that no keyring could take: see [Model tiers](#model-tiers-and-end-to-end-encryption)) | `~/.config/datoolkit/config.toml` |
| Pinned RDP and WinRM certificates | `~/.config/datoolkit/rdp_pins.json` |
| Companion certificate and key | `~/.config/datoolkit/companion-cert.pem`, `companion-key.pem` (0600) |
| Secrets (API keys, host passwords, a password written into a provider's address) | OS keyring, service `datoolkit`; only one that keeps them safe: with none running but one that writes a plain file, nothing is stored and Settings says so |
| Case logs, transcripts, exports | `~/.local/share/datoolkit/cases/<case-id>/` |
| Research reports (reused for 30 days, each with its case's client and sensitivity) | `~/.local/share/datoolkit/research/` |
| Baselines, by host | `~/.local/share/datoolkit/baselines/<host>/` |
| The tool cache | `~/.local/share/datoolkit/tools/` |

Every directory DA Toolkit makes under these two is readable by you alone (0700), and ones
made by earlier versions are tightened on the next start. The askpass socket and helper
script live in `$XDG_RUNTIME_DIR/datoolkit-<pid>` (every desktop session sets that); without
it they fall back to `/tmp/datoolkit-<pid>`, where on a machine shared with other users
someone who creates that name first stops DA Toolkit from starting, so keep
`XDG_RUNTIME_DIR` set there.

**Files are written whole.** Settings, case files, drafts and pins are each written to a
temporary file, flushed to the disk and then renamed over the old one, so a crash or a power
cut leaves the file as it was before or as it is after, never half of it. What can't be read
is set aside under a name that says so, and never overwritten:

- `config.toml.broken-<time>`: a settings file that couldn't be read, or one found empty on a
  computer where DA Toolkit has been used. DA Toolkit starts with default settings and a
  banner that names the file: fix it, quit, and put it back as `config.toml`.
- `state.json.bak` in a case folder is the state as saved one change earlier. A case whose
  `state.json` can't be read, or is missing, opens from it, and the unreadable file is kept as
  `state.json.damaged-<time>`.
- `rdp_pins.json.unreadable-<date>-<time>`: see RDP, the pin file.

The lock files (`instance.lock` and `.config.toml.lock` beside the config, `.lock` in a case
folder, `datoolkit-guacd.lock` and `datoolkit-guacd.turn.lock` in the runtime directory) are
how two running DA Toolkits keep out of each other's way (see [First run](#first-run)). They
hold nothing of yours and can be left where they are.

What is kept only in memory, and what never reaches a file or a log, is in
[Security and privacy](docs/security-and-privacy.md#what-is-stored-on-this-computer).

Inside a case directory: `case.json` (name, sensitivity, notes, client, whether its history is
used, and how many messages it has, for the case list), `events.jsonl` (audit log),
`state.json` (conversation, chat and queue, rewritten on every change so the case can be
resumed) and `state.json.bak` (the save before it), `draft.json` (what the chat box holds,
not sent yet), `requests.jsonl` (every model request as it was sent: prompt, messages,
reasoning, reply),
`img-N.png/jpg` (images as sent), `term-<session>.log` (transcripts), `transcript.md` and
`ticket-summary.md` (exports).

## Untrusted output

Everything a command prints is untrusted: a log line, a banner or a web page can carry text
meant to steer the model. These layers deal with that:

- the system prompt tells the model to treat output as data;
- text that isn't DA Toolkit's can't pass for it. DA Toolkit writes its own notes to the model
  inside the conversation, each beginning `[DAToolkit`. Wherever text from elsewhere goes to a
  model (command output, search results, fetched pages and their titles, research reports,
  an image's description, a baseline diff, a runbook, a session's target), that marker is
  rewritten as `[untrusted "DAToolkit"`, however it was disguised (other brackets, fullwidth
  letters, invisible characters inside it), and the model is told what the rewritten marker
  means. The same holds for what the research agent reads;
- the review dialog flags text that looks like an injection before you send it, and text you
  can't see but the model would read: invisible Unicode tag characters (the warning spells out
  what they say) and bidi controls, which reorder what is shown. The check reads text as a
  model would: a phrase wrapped across lines, split by a colour code or written in fullwidth
  letters is still found, in English and in German, and so is an imitation of DA Toolkit's
  own marker;
- web search results, research reports and fetched pages reach the model marked as untrusted,
  with the same check (a search's card says when its results held such text), and text no one
  can see is taken out of search results, page text and reports before the model reads them;
  the research agent works
  from the brief alone and fetches only URLs it was shown, and a page returned whole has first
  been checked by the agent, its title with it (a title reaches the model as one line of at
  most 120 characters), and counts as clean only when the agent plainly said so;
- the model's reply is text, not a page: HTML in it is shown as typed and never becomes an
  element, so a reply (or a research report, or a message from the phone) can't put a control
  of its own on the page, hide the page's, or stand in for a dialog, and `<your-domain>` in a
  sentence stays visible. Only what Markdown itself stands for is drawn, and a link is made
  only for an `http`, `https` or `mailto` address;
- the reply is rendered with no remote resources at all (no images, media or
  embeds, and a Content-Security-Policy that only allows this origin), so a steered model
  cannot leak data by making the page fetch a URL. Links open in your browser, and only when
  you click them: a steered model could still write a link whose address carries case data,
  so look at where a link goes before you follow it. A link to a plain `http://` address
  anywhere but this machine, its network or a Tor onion service is marked **⚠ not secure**
  (judged from where it really goes, so link text that reads `https://` doesn't hide it), and
  opening it first asks, naming the full address; Cancel is the default.

Local risk rules raise a command to **disruptive** when it fetches and runs code
(`curl … | sh`, `curl … | sudo -E bash -`, `bash <(wget …)`, `base64 -d | sh`,
`Invoke-Expression`, `-EncodedCommand` or `-e`, `DownloadString`), erases (`shred`, `find
-delete`, `format D:`, `vssadmin delete shadows`, `zfs destroy`, `nvme format`), deletes by other
means than `rm -r` (`find … -exec rm`, `xargs rm`, `rsync --delete`, `git reset --hard`, `git
clean -f`, `unlink`, `dconf reset -f`, `flatpak uninstall --all`), ends sessions or every process
(`loginctl terminate-session`, `kill -9 -1`), reboots through D-Bus (`busctl call … Reboot`),
opens a reverse shell or sets world-writable permissions, and to **modifying** for `eval`, inline
interpreter code (`python -c`) and switching user (`su`, `sudo -i`). A command counts wherever
it starts: on any line, after `do`, in a `{ … }` group, behind `sudo -u root`, `pkexec`, `run0`,
`systemd-run`, `env`, a variable set for it (`FOO=1`), `nohup`, `timeout -s KILL 5` or `xargs -a
list`, by its path (`/bin/rm`) or escaped (`\rm`).
