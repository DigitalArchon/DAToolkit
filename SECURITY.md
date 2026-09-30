# Security policy

## Reporting a vulnerability

Please email **jason@digitalarchon.com.au** with the details and, if you can, steps to
reproduce. Don't open a public issue or pull request for a vulnerability.

You'll get a reply as soon as I can manage. This is a one-person project with no bug bounty,
but reports are taken seriously and credited in the fix unless you'd rather not be named.

## Supported versions

DAToolkit is in beta. Only the latest release (or `master`) gets fixes.

## What's in scope

Anything that breaks one of DAToolkit's promises, for example:

- the AI (or text in command output) getting a command executed without a technician's click;
- data from a Confidential or Sovereign case reaching a model or service its tier forbids;
- secrets surviving redaction, or reaching config files, logs or exports that shouldn't hold them;
- flaws in TEE attestation, reply-signature checks or the end-to-end encrypted Private Mode path;
- bypassing the local server's tokens, or the companion view reaching more than it should;
- RDP certificate pinning being bypassed;
- a model reply that makes the page load remote resources or run script.

Weaknesses in upstream projects (NanoGPT, Tinfoil, guacd, xterm.js and so on) should go to
those projects, though a heads-up is welcome if DAToolkit can mitigate them.
