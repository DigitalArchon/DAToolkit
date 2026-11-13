# Security and privacy in DA Toolkit

This is how DA Toolkit handles your security and your clients' privacy: what leaves this
computer and where it goes, what stays, how keys and passwords are kept, what each kind of AI
model can see, and what DA Toolkit can't protect you from. It describes DA Toolkit as of
version 0.1.0b29 (November 2026). For how to use each feature, see the [manual](../MANUAL.md);
the sections named below are the manual's.

## In short

- **The AI never runs anything.** It proposes commands; each one runs only when you click Run
  or Insert, and the AI sees only the output you review and send.
- **What leaves this computer** is what you send to the AI model you chose (your messages, the
  output you send, photos after you black out what you want, the case's notes), web searches
  and research briefs you approve (or allow without asking, in Open cases only), the commands
  you run on your own hosts, and what checking an attested model takes: nonces, its evidence to
  NVIDIA, requests for Intel's public certificates. The phone companion, when you start it, gets
  the case's name, last messages and queue over your local network.
- **What never leaves**: your API keys and passwords (except to the service each is for),
  command output you don't send, the unredacted original of a photo, the case files on disk,
  and anything at all over plain `http://` to a machine outside your own network. DA Toolkit
  has no telemetry, no crash reports and no update check.
- **Case sensitivity decides which models may see a case**: Open cases any model, Confidential
  cases only end-to-end encrypted (`private/`) or local models, Sovereign cases only local ones.
  A case's sensitivity can only be raised.
- **Secrets live in the OS keyring**, never in a log, and not in the config file (with one
  exception, where no keyring is to be had: see [Keys and passwords](#keys-and-passwords)).
- **Your own network is reached directly.** A proxy named in your environment is used for what
  lies beyond this computer and its local network, never for a model server or a WinRM host on
  it.

## Every service DA Toolkit contacts

Nothing is contacted until you configure it or use the feature that needs it, and every address
is https unless it is on this computer or its local network (see [Network rules](#network-rules)).

### The AI providers you add

Settings → AI providers holds each provider's address and key (see the manual's
[AI providers](../MANUAL.md#ai-providers)). Each request to the chat model carries the system
prompt, the conversation so far, the case's state (sessions, queue, hypotheses) and the tool
definitions, with your key. The same goes, each with its own purpose, to the vision helper (an
image and the message it came with), the second-opinion reviewer (the command with its risk
label, stated purpose and rollback, what the local rules flagged on it, the case's name and
notes, the session it is for and the hypotheses; never the conversation or the proposer's
reasoning), the research agent (its brief, never the case), compaction and write-ups (the
conversation). A password you typed into a queued command is blanked wherever that command
goes to a model: the chat model's view of the queue, a result's header, the reviewer's
request. DA Toolkit also asks each provider for its model list, NanoGPT and OpenRouter
for their models' details (with your key), and NanoGPT for a model's hosts when you open its
route (with no key). What a provider can read of a request depends on the model's tier (see
[AI models](#ai-models)).

Which model sees a case is your choice, within the case's sensitivity: DA Toolkit refuses to
send a case to a model its sensitivity forbids. A provider is sent its own key and nothing
else of your environment: the variables the OpenAI and Anthropic libraries would read by
themselves (`OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_CUSTOM_HEADERS`,
`ANTHROPIC_CUSTOM_HEADERS`) add no header to a request.

### NanoGPT's other services

With a NanoGPT provider (`nano-gpt.com`), DA Toolkit also uses, with the same key:

- **Web search** (`/api/web`): each approved query, after redaction, goes to NanoGPT, which
  passes it to the search provider you chose in Settings → General (Perplexity, Kagi, Linkup,
  Valyu, Tavily, Brave, Sofya, Firecrawl or Exa) in the clear. See
  [Web search and research](#web-search-and-research).
- **The page scraper** (`/api/scrape-urls`), for the research agent's page fetches. NanoGPT
  fetches the pages; DA Toolkit itself never loads a web page.
- **TEE attestation and reply records** (`/tee/attestation`, `/tee/signature/…`), for `TEE/`
  models: our random nonces go out, the enclave's evidence and each reply's signed record come
  back.
- **Private Mode's relay** (`/api/v1/private/tinfoil`), for `private/` models: the sealed
  request and reply, the enclave's attestation bundle, a billing check that carries only the
  model and the request's size, and the public list of Private Mode models (fetched with no
  key).

### Attestation services

Checking that a model's enclave is genuine contacts, with nothing of your case in any of it:

- **Intel** (`api.trustedservices.intel.com`, `certificates.trustedservices.intel.com`) for a
  TEE model: its public certificates, revocation lists and platform ratings, to check the
  enclave's quote. Kept for an hour, once they have been read and their signatures checked.
- **NVIDIA** (`nras.attestation.nvidia.com`) for a TEE model: the GPU evidence from the
  enclave's own report, for NVIDIA's signed verdict, and NVIDIA's signing keys (kept for an
  hour).
- **Tinfoil's verifier**, for a `private/` model, run by Tinfoil's own SDK (pinned to an exact
  version): Tinfoil's mirrors of GitHub (`github-proxy.tinfoil.sh`, the router's latest release
  and its Sigstore bundle), of AMD's and Intel's certificate services (`kds-proxy.tinfoil.sh`,
  `tdx-proxy.tinfoil.sh`), and Sigstore's public trust root (`tuf-repo-cdn.sigstore.dev`, which
  the Sigstore library keeps under `~/.local/share/sigstore-python` and
  `~/.cache/sigstore-python`). DA Toolkit holds every one of these fetches, the Sigstore
  library's included, to its rule for redirects: one is followed only to https, never from the
  internet into this computer or its network (for Sigstore's trust root, five at most). One it
  may not follow fails the attestation, and nothing is sent; should a version of that software
  not let DA Toolkit hold its fetches to that, the attestation is refused, saying why, rather
  than made without it.

### RedPill, only if you ask (`api.redpill.ai`)

**Name the provider behind each TEE reply** (Settings → Model → TEE models) is **off by
default**. With it on, after a TEE reply whose receipt holds, DA Toolkit asks RedPill's public
API which provider ran the model (see [TEE models](#tee-models-tee)). That request goes to
`api.redpill.ai` directly, not through NanoGPT, so **RedPill sees this computer's internet
address and that one of its models was used around then**. It carries the session's id and
nothing else: never your messages, never a key. Each session's record is fetched once while DA
Toolkit runs (a session serves a provider for about an hour), at most a megabyte, and never for
a model Chutes serves. Each fetch is recorded in the case's audit log and timeline: the host,
the session id, and what came back.

### Other providers

**Anthropic** (`api.anthropic.com`), **OpenRouter**, **WaveSpeed** and any other provider are
contacted only when you add them, for their model lists and the requests you send. With
Anthropic's own web search chosen (Settings → General, Open cases only), Claude searches on
Anthropic's side while it answers, at most four times a turn, so those queries aren't shown to
you first; DA Toolkit shows them, with their results, on the AI's message.

### Your own hosts

SSH, WinRM and RDP sessions connect to the hosts you add, and what you run goes there (see
[Sessions](#sessions)). guacd, the RDP gateway, is reached only on this computer (127.0.0.1).

### Your phone

The phone companion listens on your local network only while you have it started (see
[The phone companion](#the-phone-companion)).

### What DA Toolkit never contacts

No analytics, telemetry, crash reporting or update checks. The AI can't fetch a web page: a
search goes through NanoGPT, a page through the research agent and NanoGPT's scraper, and a link
in a reply opens in your browser only when you click it. DA Toolkit never downloads the guacd
container image itself; you pull it once, and it runs exactly the pinned image.

## Network rules

- **https, or nothing.** Every address is https unless it is on this computer or its local
  network, which is how a model server of your own is reached: `localhost` and names ending
  `.localhost` or `.local`; `127.x` and `::1`; `10.x`, `172.16`–`31.x` and `192.168.x`;
  link-local addresses (`169.254.x`, `fe80::…`); and IPv6 unique local addresses (`fc00::/7`).
  Plain http to anything else is refused, with the reason, before a byte is sent: Settings
  won't save it, and a config file edited by hand to hold one sends nothing. A name ending
  `.lan` is not on that list: it counts as a Local model server (see
  [Local models](#local-models)) but is reached over https only. There is no exception for
  onion addresses: a provider on one must be https. The address is never rewritten to https for
  you, and it is judged whole, as written, never looked up to see where it points:
  `http://127.0.0.1%2e.example.com/` is a name under example.com, not this computer.
- **Certificates are always verified.** Nothing in DA Toolkit turns verification off. A WinRM
  listener with a self-signed certificate is checked by DA Toolkit itself, pinned on first use
  after you have verified it, as RDP certificates are.
- **Redirects.** A model provider's redirect is never followed, since it would send the
  conversation and the key again to wherever it points; the error says where it pointed. The
  attestation clients, a private model's (Tinfoil's and Sigstore's) included, follow no redirect
  off https, nor one from the internet into this computer or its network (an attestation
  endpoint must not reach your router's admin page).
- **An answer that isn't the API** (a web page, a network's sign-in page, an address without its
  `/v1`) is an error naming what came back, never taken for an empty reply.
- **A reply that was cut off is not a reply.** When a provider's connection closes before it
  has said the reply is complete, the turn fails with an error and nothing in what arrived is
  queued.
- **Proxies.** DA Toolkit has no proxy or Tor setting of its own. Its connections go through
  the proxy your environment sets (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`), if any, with
  one exception: **what is on this computer or its local network is reached directly**, so the
  key and the case never pass a proxy on their way to a machine of your own. That holds for a
  model provider at an address the Local tier counts, for attesting a model at one of the
  addresses plain http is accepted for, and for a WinRM host at one of those, where Basic
  authentication over plain HTTP would otherwise hand the proxy the password. Everything
  further away (cloud providers, NanoGPT's services, the attestation services, a WinRM host
  elsewhere) goes through the proxy when one is set. SSH is your system's `ssh` and follows
  its own configuration; RDP goes through guacd on this computer.
- **The app's own pages** are served on 127.0.0.1 only, never on the network, and every answer
  says that it may not be framed by another page, read as another type of content, or named
  in a referrer.

## Keys and passwords

- **The OS keyring holds every secret**: API keys, host passwords and a password written into a
  provider's address, under the service `datoolkit`. Only a keyring that keeps them safe is
  used (GNOME Keyring, KWallet and the like): with none running but one that writes a plain
  file, nothing is stored and Settings says so. A keyring that can't be used when a provider
  or a host is saved, renamed or deleted (it is locked, or its prompt was dismissed) leaves
  everything as it was, with a message: nothing is half changed.
- **`config.toml` holds no secrets**, with one exception. A user name and password written
  into an address (`https://user:pass@host/v1`) go to the keyring, and the file has the address
  without them; they are put back only for that same address, so editing the file to point at
  another server doesn't hand it the password. An address with a password in it is shown and
  logged as `https://…@host/v1`; the app's own page is given it whole, because the provider's
  form in Settings shows the address for you to edit. The exception: a password found in a
  `config.toml` edited by hand, or written by an earlier version, is moved to the keyring and
  out of the file when the file is read, but where no keyring can take it, it stays in the
  file and is used from there.
- **A key goes only to the service it is for.** The NanoGPT key goes to NanoGPT (chat, search,
  the scraper, attestation); no key goes to RedPill, Intel, NVIDIA or Tinfoil's mirrors. A key
  with a space, a line break or another character no key has is refused, saying so without
  quoting it.
- **Host passwords** are typed into ssh's prompt through DA Toolkit's askpass bridge, given to
  pypsrp for WinRM, or handed to guacd for RDP by DA Toolkit itself, so the password never
  reaches the page. The helper that carries a password, and the WinRM console, import nothing
  from the folder they run in (your home folder).
- **A saved password goes only to the host it was saved for.** For SSH it answers only a
  password prompt that names the session's own host in text no server can write (OpenSSH's own
  prompt, or the `(user@host)` it puts before a server's words), and never a jump host's,
  whatever that prompt says. Behind a jump host, a prompt naming no host is shown to you
  instead, and so is every prompt where this computer's OpenSSH is older than 8.4, which
  doesn't say which server worded one.
- **A password is saved once the host has accepted it**, not when you type it: ticked "Save to
  keyring for this host", it is held in memory until the login has succeeded, and dropped if
  the host refuses it or the session ends first. When a WinRM or RDP server refuses a password
  the keyring supplied, DA Toolkit says that a saved one was used and how to replace it.
- **What never reaches a file or a log**: keys and saved passwords. What does: `requests.jsonl`
  keeps every model request as it was sent (the output you reviewed and sent, not raw terminal
  output), `events.jsonl` the case's audit trail, and `term-<session>.log` each terminal's raw
  transcript, unredacted, which is why the full export leaves transcripts out unless you ask.
  A password you type into a command (`sshpass -p …`) is part of that command: it is blanked
  where the command goes to a model, and stays as typed in the queue, the audit trail and the
  transcript.
- **Plain http on your own network carries the key in the clear.** A Local model server on
  `http://192.168.x.x` gets your key and the case unencrypted across the LAN. DA Toolkit allows
  it for your own network only, and doesn't ask first (see [Limits](#limits)).

## AI models

| Tier | Which models | What the provider can see |
|---|---|---|
| Standard | any other | everything |
| TEE | `TEE/…` and `phala/…`; also an id with `tee` as one of its `/` parts, or ending `-tee` or `:tee` | everything, in the clear on NanoGPT's servers; the model is attested before anything is sent |
| E2EE | NanoGPT `private/…` | ciphertext only, plus your account, the model, timing, sizes and usage |
| Local | any model at `localhost`, a local-network address, or a name ending `.local` or `.lan` | nothing leaves your network |

| Case sensitivity | Models allowed |
|---|---|
| Open | Standard, TEE, E2EE, Local |
| Confidential | E2EE, Local |
| Sovereign | Local only |

A model's tier can be overridden (Standard, TEE or Local), never to E2EE: only a `private/`
model is sealed. Marking a cloud model Local lets Confidential and Sovereign cases use it, so
do that only for a model on your own hardware. See the manual's
[Model tiers and end-to-end encryption](../MANUAL.md#model-tiers-and-end-to-end-encryption).

### Standard models

Everything goes to the provider as written, over https. Their privacy terms are theirs. A
NanoGPT route picks which host serves a model, and each host's terms are its own.

### End-to-end encrypted models (`private/`)

NanoGPT's Private Mode runs the model in an enclave Tinfoil operates. DA Toolkit uses Tinfoil's
own verifier, pinned, never NanoGPT's `npx` proxy, whose code NanoGPT could change.

1. **Attested before anything is sent**, even NanoGPT's billing check: the enclave's hardware
   report must match the release Tinfoil published, as recorded by Sigstore, and bind the key
   requests are sealed to. A release older than Tinfoil's latest is flagged, not refused. The
   attestation is made again every five minutes, and at once when you click to re-check; a
   re-check that fails leaves none in force.
2. **Sealed on this computer** to that key: the whole request body, conversation, tools and a
   private prompt-cache secret. NanoGPT's relay carries ciphertext.
3. **Sealed replies only.** A reply that isn't sealed by the enclave, isn't marked Private
   Mode, or whose stream ends early is refused, whatever its HTTP status. What the relay writes
   in an error is unauthenticated, so it is shown as the relay's, on one line, without
   invisible characters. A `private/` model is never sent in the clear, never falls back to
   the plain endpoint, and a model NanoGPT no longer offers is never sent to. A provider set
   to the Anthropic API, which has no sealed path, lists no `private/` models and is refused
   one.

What NanoGPT still sees: your account, the model, when, how much, and the sizes.

### TEE models (`TEE/`)

A TEE model runs behind hardware attestation, but **your text passes NanoGPT's servers in the
clear**, and the gateway or provider that serves the model, which is why TEE models stay out of
Confidential cases. What DA Toolkit checks:

- **Attested before anything is sent.** The enclave's Intel TDX quote must bind its keys to a
  fresh random nonce of ours, read from the quote's own bytes; the quote must verify up to
  Intel's root (pinned in the code), with Intel's revocation lists and ratings, and not be a
  debug enclave; GPU evidence must get NVIDIA's signed verdict for our nonce. Anything forged,
  revoked, for another nonce, relayed for a nonce the relay chose, or with expired keys,
  evidence that can't be read, an attestation not done within three minutes, and a model that
  offers no attestation at all, is **refused**, and nothing is sent. What a refusal quotes of
  the relay is shown on one line, short and without invisible characters.
- **Partial** when nothing failed but something couldn't be checked or falls short. The top bar
  says so and names each shortfall. Sending still goes ahead: DA Toolkit has no setting that
  refuses a partial attestation.
- **What is attested** is said in the top bar and its tooltip, with what is someone's word:
  - For every model RedPill serves (most `TEE/` models), what is attested is **RedPill's TEE
    gateway** ("🛡 TEE gateway attested"). The gateway passes each request on to the machine
    that runs the model (Phala's, Fortanix's, NEAR AI's, 0G's, Tinfoil's router or Chutes'), and
    that machine is **the gateway's word**, in each reply's signed receipt: DA Toolkit doesn't
    attest it. The GPU evidence NanoGPT's report carries is made for our nonce, but the
    gateway's quote doesn't commit to it, so it falls short as **GPUs not tied to the quote**:
    they may be another machine's.
  - For a model Chutes serves itself, **each instance** Chutes lists is attested, each with its
    own GPUs, and any instance that fails refuses them all. Chutes signs no replies, so nothing
    shows which instance answered.
  - A model whose own evidence can't be had for our nonce, but which RedPill's gateway answers,
    is **attested through the gateway alone**: its own enclave is then entirely the gateway's
    word, and each reply is verified only with a receipt that holds.
- **Attested again** before a send once the attestation is 15 minutes old (time asleep counts),
  or once a reply has failed its check. **Nothing is sent while a model is being attested**, or
  after its attestation was refused: a request waits for the attestation under way (and for a
  re-check begun while it waited) and goes by what it finds, and a refused one leaves no
  earlier one in force. Every path takes this guard, before each request it makes and not only
  its first: the chat (every round of a turn), the vision helper, write-ups, second opinions
  (automatic ones too), compaction and the research agent (every request of a run).
- **An attestation is of one address.** Change a provider's address, delete the provider or
  choose another model, and the top bar shows no attestation until the model in use has been
  attested as it is now; one still under way for the old address is not kept for the new. A
  new API key for the provider has its TEE models attested again before the next request.
- **Each reply is checked** against the record the attested key signed for it, every reply of
  a turn or a research run, each against the attestation its own request went out under, and
  marked:
  - **✔ verified**: signed by an attested key, for the exact bytes DA Toolkit received, the
    gateway's receipt (where read) holding. The mark's tooltip says whether a receipt was read:
    a model attested itself is verified by its own key's signature with or without one, and
    without one nothing says the machine running the model was verified;
  - **signed, text not compared**: signed by it, but NanoGPT rebuilds this model's replies;
  - **⚠ reply not verified**: no record could be read (also when the reply named nothing to
    look its record up by, or was cut short), or, through the gateway alone, the record
    came without a receipt that holds. Not a failed check, but nothing shows the reply came from
    the enclave attested. A turn or a run takes the mark of its worst reply;
  - **✖ check failed**: another key signed it, or it isn't the bytes signed, or its receipt
    doesn't hold. Said in a notice, and the model is attested again before anything more is
    sent to it;
  - **unsigned**: the provider signs no replies (Chutes).

  Your own messages carry no TEE mark in DA Toolkit: what reached the enclave is shown only by
  the reply's mark.
- **Who ran a reply**, optionally (off by default): with **Name the provider behind each TEE
  reply** on, the receipt's session is looked up on `api.redpill.ai` (see
  [RedPill](#redpill-only-if-you-ask-apiredpillai)), believed only if its record is exactly the
  one the signed receipt names and the reply was served within the session, and shown beside
  the mark ("✔ verified · run by Phala"). That name is RedPill's word: DA Toolkit doesn't check
  the machine it names, and a record that can't be read names nobody. The mark never changes.

What attestation doesn't prove: which software the enclave runs (its measurements aren't
compared with published ones), what was sent (the record hashes the request as NanoGPT
forwarded it), or, for models RedPill serves, the machine that runs the model.

### Local models

A model counts as Local by its provider's address alone: `localhost`, an address plain http is
accepted for (see [Network rules](#network-rules)), or a name ending `.local` or `.lan` (the
last over https only). Nothing leaves your network, but over plain `http://` the key and the
case cross your LAN unencrypted, and a `.local` or `.lan` name is only as trustworthy as your
network's name resolution. Put the model server behind https if the network isn't yours alone.
A Local provider is reached directly, never through a proxy from your environment.

## Web search and research

- **Gated by you.** By default each query, and each research brief, appears for you to edit,
  approve or skip. "Search without asking" applies to Open cases only; Confidential cases always
  ask, and search only with Linkup, which keeps no data, with no fallback; Sovereign cases never
  search or research.
- **Screened before it goes.** Secrets are blanked out of a query; characters no one can see are
  taken out first; a query or brief that says who or where a computer is waits for you even
  when searching without asking. That means an e-mail address, a MAC address, an IP address
  that isn't a public one (IPv4 or IPv6), the server of a `\\server\share` path, this
  computer's own names (your login and real name, the host name), the case's client, and the
  host, domain and login of each open session to another machine, each found as it may be
  written (`dc01` for `dc01.acme-corp.local`). Words too common to name anyone (`root`,
  `server`) are not looked for. The research agent's own searches that name one are refused,
  except what the brief you approved names itself.
- **The research agent sees only the brief**, never the case, and may be a Standard-tier model
  even in a Confidential case, where every brief waits for you. It fetches only addresses that
  came from its search results or the pages it read, so a hostile page can't send it to an
  address of its own making; a page returned whole is first checked for text aimed at an AI,
  and such passages are cut out.
- **A kept report stays with its client.** Reports are kept on this computer for 30 days, each
  with the client and sensitivity of the case it was written for, and reused for the same
  question only by a case of the same client (or between cases that have none) that is at least
  as sensitive, and only after you have approved the brief where briefs are asked about. One
  whose TEE check failed isn't kept.
- **Search results and reports reach the AI marked as untrusted**, with text no one can see
  taken out and text that looks like instructions flagged. What the research agent reads of
  the web is cleaned the same way.

## Commands and what the AI sees

- **The AI only proposes.** Proposals land in the queue with a risk label (read only, modifying,
  disruptive) that local rules can raise but never lower, and a separate **sensitive** flag for
  commands that may expose secrets. Disruptive commands need a second confirmation, and a
  command that would cut the session it runs in says so. Only your click types a command. The
  AI can withdraw or reorder only what it queued itself, never a recipe's steps, a tool
  transfer, a rollback or a dry run that you queued. A tool call a model writes as text is
  taken for a call only where it ends the message, never where the model quotes one, and
  never from a Claude model. A call with an item that can't be read queues nothing.
- **Read only is the local rules' word, not the AI's.** Read only is the label that runs on
  the keyboard shortcut and can be watched, so it stands only for a command the local rules
  know as one that only reads: a named reader in a form that reads, with nothing on the line
  the rules can't see (an operand hidden in an unquoted expansion, text handed to another
  shell or an interpreter, output sent to a file, a command too long to read in full, a
  statement that begins with a variable anywhere but in a WinRM session, where only
  PowerShell can be reading it). A
  command the AI calls read-only that the rules don't know is shown as modifying, said to be
  unknown to them, and can't be run by the shortcut or watched. A dry run is rated by the same
  rules as any other command, so a rehearsal is read only only where the rules confirm it
  changes nothing, and none is offered where part of the command would still act. A recipe's
  step, which is its author's text and not the AI's, keeps its author's read-only label where
  the rules have nothing against it. When a case is opened again, its pending commands are
  rated again by the rules in force, never below the AI's label.
- **What you see is what runs.** Control characters, bidi overrides, zero-width and tag
  characters and look-alike spaces are taken out of every command before it is queued, and the
  item says what was removed. The command box shows all of its command (it scrolls, and says
  how many lines there are, when the command is long), and Run types the command as the
  server holds it: an item that changed since you were shown it, or a box you emptied, types
  nothing. Run group always lists what it will type first. Control characters are also left
  out of what you paste into a terminal.
- **You choose what goes back.** Results are captured per command, redacted and shown to you to
  edit before they are sent; the review warns of text that looks like an injection, and of
  invisible characters. Redaction blanks secrets that something names (a setting called
  password, secret, token or key, in the forms configuration files, devices, Windows and
  command lines write it; private keys; password hashes; well-known tokens). It doesn't blank
  a secret nothing names (a bare base64 string, a password in a table or a sentence), nor
  names, addresses or other personal data. It is a safety net, not a guarantee: the review is
  what you rely on.
- **Text that isn't DA Toolkit's can't pass for it.** DA Toolkit's own notes to a model begin
  `[DAToolkit`; in everything else that goes to a model (output, search results, pages and
  their titles, reports, a window's title, runbooks) that marker is rewritten as
  `[untrusted "DAToolkit"`, and the model is told what that means.
- **Images carry nothing but pixels.** Every image goes through a redaction editor (black boxes
  replace the pixels, so the original is never stored or uploaded), and the server re-encodes it
  from its pixels alone: no EXIF, GPS, thumbnails, comments or file names.
- **Replies can't load anything, or change the page.** HTML in a reply (or in a research
  report, or a message from the phone) is shown as text and never becomes an element, so it
  can't put a control of its own on the page or hide one of the page's. A reply is rendered
  with no remote images, media or embeds
  under a Content-Security-Policy that allows only this app, so a steered model can't leak data
  by making the page fetch an address. A link opens in your browser only when you click it, and
  a link to plain http anywhere but this computer, its network or an onion service is marked
  **⚠ not secure** and asks first. A link in a terminal's output opens the same way, after a
  dialog that names its address in full. In the Markdown transcript, text from the web and
  from models is written so that it is no markup of its own.
- **A case's history stays with its client.** Runbooks from earlier cases reach the AI only for a
  case that belongs to a client and has "Include this client's history" ticked, only from that
  client's cases, and only from cases of the same or a lower sensitivity; they are given as
  text from an earlier case, not as instructions. Baselines, which are filed by host, follow
  the same two rules: a case can't diff against a baseline taken for another client or in a
  case of a higher sensitivity, and both snapshots are redacted before they are compared.
- **A case never comes back less protected.** A case whose `case.json` is lost or unreadable is
  opened at the highest sensitivity its audit log ever recorded, and as Sovereign where the log
  can't say.

## Sessions

- **SSH** uses your system `ssh`, so your keys, agent, `~/.ssh/config` and `known_hosts` apply.
  Password and host-key prompts appear as dialogs; a saved password is supplied once per session
  through an askpass bridge in your runtime folder, and only to the host that is asking, never
  to a jump host (see [Keys and passwords](#keys-and-passwords)). A prompt whose program has
  gone is taken off the screen at once.
- **WinRM** accepts plain HTTP only on this computer and its local network, and Basic
  authentication over it only with **Allow Basic over plain HTTP** ticked on the host (the
  password then crosses the network in the clear; NTLM and Kerberos protect it themselves).
  Such a host is connected to directly, never through a proxy from your environment.
  Certificates are verified, or pinned by DA Toolkit after you check them.
- **RDP** goes through guacd on 127.0.0.1:4822 only, since that link is unencrypted, and only to
  a guacd that is yours or the system's: one another user of the computer started is refused.
  DA Toolkit does the connection handshake itself, so the password never reaches the page. The
  server's certificate is read by DA Toolkit before every connection, shown for you to verify the
  first time (its fingerprints first, above the subject and issuer, which are text the server
  chooses and are shown on one line each), and pinned; a different one blocks the connection,
  and so does a pinned server that now offers no TLS at all. "Negotiate (any)" connects with
  what the server chose when its certificate was read, so a downgrade to legacy RDP security is
  refused. A pin file that can't be read is set aside and said to be, never taken for "no
  pins". Where DA Toolkit runs guacd's container, it runs it with no capabilities and no way to
  gain privileges, from the image you pulled (`--pull=never`).
- **Typing into a remote desktop** is key presses: a command with a tab or another control
  character is refused, Insert of a command of several lines asks every time (each line break
  is Enter there), and your own keys are held back while a command is typed.
- **Window sessions** (ScreenConnect and the like) never type into the window: the AI sees only
  screenshots you take and black out, and you paste its commands yourself. A session knows its
  window by its process and class as well as its id, so another window that is later given the
  same id is not shown in its place.
- **Linked sessions** are told to the AI as one machine, so sessions are linked by themselves
  only when they reach the same machine: the same address by the same jump hosts, and for a
  loopback address the same port. A saved host at a loopback address is never linked to your
  local shell.

## The local app and the phone companion

### The app

The app's pages are served on 127.0.0.1 with a token made at each launch. The browser is opened
on a one-time code, good once and for two minutes, never the token itself, since what a browser
is opened with is on its command line for every user of the computer to read. The address
printed for opening by hand carries the token after a `#`, which the browser never sends: treat
it like a password, since it grants terminal access. A page whose token the app refuses (the
app was started again since) says so and shows nothing else.

Two DA Toolkits can run at once, a case each. A case is open in one of them at a time, and
neither saves its settings over the other's newer ones: the one that is behind is refused,
with a message, and changes nothing in the file or the keyring. A `config.toml` edited by hand
while the app runs is likewise not overwritten.

### The phone companion

Started from **📱**, it serves a phone page over HTTPS on one port of your local network (48443
by default), with a self-signed certificate kept for this install. It listens on one address,
the one its pairing codes carry (this computer's address on the network its default route
leaves by), so it is not also offered on a client's VPN or a container bridge; only a computer
with no such address has it listen on every interface.

- **Pairing takes two scans.** The first opens a page with no secret on it, to compare the
  certificate's SHA-256 fingerprint with the one on the computer. The second carries a pairing
  code, good once for five minutes, in the address's `#` part, which the browser never sends.
  The computer then asks **Allow this phone?** with a four-digit number the phone also shows;
  Refuse is the default. An allowed phone gets a token only it has; DA Toolkit keeps a hash.
- **What the phone gets**: the case's name and sensitivity (not its notes, its client or its
  folder), the last 30 chat messages, the queue with its commands, the hypothesis board and
  the list of sessions (id, kind and target). It can mark queue items and hypotheses, and send
  a photo (cleaned of metadata, under the case's rules). Its "pending" undoes its own ran or
  skipped; a command the AI withdrew can be restored only at the computer. It can't reach a
  terminal, run anything, open sessions, or see settings, prompts or credentials.
- **Every answer** says it may not be cached, framed or sent on as a referrer, and loads nothing
  from elsewhere. A request without a phone's token is refused before its body is read.
- The chat does go to the phone: think twice before pairing on a Confidential case.

## What is stored on this computer

| What | Where |
|---|---|
| Config: providers, hosts, clients, settings; no secrets (but see [Keys and passwords](#keys-and-passwords)) | `~/.config/datoolkit/config.toml` |
| Pinned RDP and WinRM certificates | `~/.config/datoolkit/rdp_pins.json` |
| The companion's certificate and key | `~/.config/datoolkit/companion-cert.pem`, `companion-key.pem` |
| Secrets | the OS keyring, service `datoolkit` |
| Cases: audit log, conversation, requests to models, images, terminal transcripts, exports, drafts | `~/.local/share/datoolkit/cases/<case-id>/` |
| Research reports, 30 days, each with its case's client and sensitivity | `~/.local/share/datoolkit/research/` |
| Baselines, by host, each with its case's client and sensitivity | `~/.local/share/datoolkit/baselines/` |
| The tool cache | `~/.local/share/datoolkit/tools/` |
| Sigstore's trust root (Tinfoil's verifier) | `~/.local/share/sigstore-python`, `~/.cache/sigstore-python` |

Every folder DA Toolkit makes under its two is readable by you alone. **Kept in memory only**,
and gone when DA Toolkit quits: the launch token and codes, phone pairing codes and the paired
phones' token hashes, attestations and their keys, RedPill's session records, the model
lists read from providers, and a password you ticked to save, until its host has accepted it.
Exports go where you save them; a copy stays in the case folder.

Settings, case files and pins are written whole (to a temporary file, flushed, then renamed),
so a crash leaves a file as it was or as it is, never half. A file that can't be read is set
aside beside itself and never overwritten: `config.toml.broken-<time>`,
`state.json.damaged-<time>` (the case then opens from `state.json.bak`, the save before it) and
`rdp_pins.json.unreadable-<date>-<time>`. These copies hold what the originals held, so delete
them when you no longer need them.

## Limits

DA Toolkit protects the path from your screen to the model and to your hosts. It can't protect
against:

- **A compromised computer.** It assumes the computer it runs on is yours and secure. Someone
  with your account can read the case files, the unlocked keyring and the address printed with
  the token.
- **What you approve.** Risk labels, reviews and redaction are aids, not guarantees: read every
  command before you run it, and every result before you send it. The risk rules read one
  command line at a time, as text: they don't follow a variable, an alias or a function that
  an earlier command left in the shell, and a modifying command runs on one click, with no
  dialog. A rehearsal that asks a tool not to act (`-WhatIf`, `--dry-run`) depends on the tool
  obeying. Redaction doesn't blank a
  secret that nothing names, or names and addresses at all. A steered model can still
  write a link whose address carries case data; it opens only when you click it.
- **A proxy in your environment.** Everything that goes beyond your own network goes through
  it when one is set, and its operator sees which services you reach and when (the content is
  https). DA Toolkit keeps only this computer and its local network off it.
- **The provider you choose.** A Standard model's provider reads everything. A TEE model's text
  passes NanoGPT, and whoever serves the model, in the clear, and for a model RedPill serves the
  machine that runs it is RedPill's word. A `private/` model's metadata (account, model,
  timing, sizes) is NanoGPT's to see.
- **Searches in the clear.** Approved queries and research briefs reach the search provider,
  and the research model, as written.
- **Your local network.** Plain http to a Local model server or a WinRM host on it crosses the
  LAN unencrypted, a `.local` name is as trustworthy as your network's name resolution, and DA
  Toolkit doesn't ask before a key goes over plain http there.
- **Pairing depends on you comparing the fingerprint.** Anyone holding a paired phone's token
  can read what the phone page shows and give the AI text (a skip's note, a photo's
  description) and photos, though the AI can't act without your click.
- **No keyring.** On a computer with no keyring that keeps secrets safe, nothing is stored for
  you, and a password that was already in `config.toml`'s provider address (put there by hand
  or by an earlier version) stays in that file.
- **An old OpenSSH.** Before 8.4, ssh doesn't say which server worded a prompt, so behind a
  jump host DA Toolkit can't tell whose prompt it is: it supplies no saved password there and
  shows you every prompt. That goes for a jump host set in the host's **Jump host** field; one
  set only in `~/.ssh/config` DA Toolkit can't see, and with so old an ssh its prompt that
  names no host would be answered with the target's saved password.
- **Multi-line commands.** Insert pastes a command with several lines as one block; a shell
  without bracketed paste runs each line as it arrives.
- **guacd 1.3** (Ubuntu's and Mint's package) can't check the pinned certificate itself, so DA
  Toolkit's check and guacd's connection are separate TLS sessions. DA Toolkit reads the
  certificate again before each connection the tab makes (a Reconnect or a page reload too),
  but someone who can answer for the server between that check and guacd's connection is not
  caught. A server offering only
  legacy RDP security has no certificate to check: you are warned on every connection.
- **The askpass socket** falls back to `/tmp/datoolkit-<pid>` where `XDG_RUNTIME_DIR` isn't set;
  on a computer shared with other users, keep it set.

## Reporting a vulnerability

Please report it privately as described in [SECURITY.md](../SECURITY.md), not in a public issue.
SECURITY.md also lists what counts as one.
