# Security policy

## Reporting a vulnerability

Please email **jason@digitalarchon.com.au** with the details and, if you can, steps to
reproduce. Don't open a public issue or pull request for a vulnerability.

You'll get a reply as soon as I can manage. This is a one-person project with no bug bounty,
but reports are taken seriously and credited in the fix unless you'd rather not be named.

## Supported versions

DA Toolkit is in beta. Only the latest release (or `master`) gets fixes.

## What's in scope

Anything that breaks one of DA Toolkit's promises, which
[docs/security-and-privacy.md](docs/security-and-privacy.md) sets out in full, for example:

- the AI (or text in command output) getting a command executed without a technician's click,
  or a click typing anything but the command the queue showed, whole;
- data from a Confidential or Sovereign case reaching a model or service its tier forbids, a
  case reopening at a lower sensitivity than it had, or a baseline or a kept research report
  reaching a case of another client or of a lower sensitivity;
- secrets surviving redaction, or reaching config files, logs or exports that shouldn't hold them;
- a web search or research brief that names the case's client, one of its hosts or logins, an
  address or an e-mail leaving without your approval, or a command the local rules rate lower
  than what it does;
- a saved host password given to any host but the one it was saved for (a jump host on the
  way, say), or saved before the host accepted it;
- a key, a password or a case passing through a proxy from the environment on its way to a
  model server or a WinRM host on this computer or its local network;
- flaws in TEE attestation (a model's own evidence, or RedPill's gateway's report it may be
  attested through), reply-signature checks (a TEE model's reply going unchecked anywhere: the
  chat, an image's description, a write-up, a second opinion, compaction or research; checked
  against a key no attested quote binds; or verified, through RedPill's gateway alone, without a
  receipt that holds) or the end-to-end encrypted Private Mode path;
- anything reaching a TEE model while its attestation is under way or after it was refused, or
  a mark or the top bar claiming more than was attested (RedPill's gateway is not the machine
  that runs the model);
- the optional TEE provider lookup (Settings → Model) sending api.redpill.ai anything but a
  session's id, sending it anything at all while the setting is off, or naming a provider for a
  record the reply's signed receipt doesn't name;
- bypassing the local server's tokens (or the one-time code a browser is opened with), the
  companion reaching more than it should, or a way around its pairing (the certificate check
  before a phone pairs, and the computer's approval of each phone);
- a password written into a provider's address reaching the config file, a message or a log;
- the app handing guacd's link (the RDP host, user and password) to another user's listener;
- RDP or WinRM certificate pinning being bypassed (a pinned server let through with another
  certificate, or with none);
- a model reply, a research report or a phone's message that makes the page load remote
  resources or run script, or that puts an element of its own on the page or hides the page's.

Weaknesses in upstream projects (NanoGPT, Tinfoil, guacd, xterm.js and so on) should go to
those projects, though a heads-up is welcome if DA Toolkit can mitigate them.
