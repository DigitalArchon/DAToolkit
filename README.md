# DA Toolkit

A gated AI diagnostic console for Linux. You describe a problem; the AI proposes commands;
**you** decide what runs and what output the AI gets to see.

> [!WARNING]
> **Beta.** DA Toolkit is beta software and is not intended for production use. Use it at
> your own risk. It helps you run commands on real systems, so read every command before
> you run it: the risk labels and reviews are aids, not guarantees.

![DA Toolkit working a disk-full case on a Linux server: the AI's hypotheses and proposed commands on the left, the SSH session on the right, the command queue with risk badges below](docs/screenshot.png)

<sub>A training scenario: the scripted training model and a mock host, no real systems.</sub>

## What it does

- **The AI cannot execute anything.** Its proposals land in a queue with a risk badge (read
  only, modifying, disruptive) and a flag for commands that may expose secrets. Only your
  click types a command into a terminal.
- **You choose what goes back.** Output is captured per command, secrets are redacted, and
  you review and edit the exact text before the AI sees it.
- **Real terminals.** Local, SSH and WinRM sessions are real PTYs (`sudo`, Ctrl-C, pagers
  and device CLIs behave normally), and RDP opens in a tab through Apache Guacamole's guacd.
- **Works through ScreenConnect.** A window session follows a ScreenConnect (or TeamViewer,
  AnyDesk, VM console) window on your screen (X11): the AI sees it through screenshots you
  send, and you paste the commands it proposes. No setup or API needed.
- **Any OpenAI-compatible model**: NanoGPT, Ollama, LM Studio, vLLM and others. See
  [which models work well](docs/model-evaluation.md) for how nine popular models did.
- **Privacy tiers per case.** Open, Confidential and Sovereign cases limit which models may
  see the data: anything, end-to-end encrypted or local, or local only. End-to-end encrypted
  and TEE models are attested before anything is sent.
- **An explicit investigation**: a hypothesis board the AI keeps up to date, recipes for
  common checks, baselines and diffs, dry runs, rollbacks and second opinions from a
  reviewer model before risky changes.
- **A record of the case**: an audit log, terminal transcripts, a Markdown export and
  AI-written ticket summaries, client updates and runbooks.
- Secrets live in the OS keyring (GNOME Keyring / KWallet), never in config files.

## Who it's for

MSPs and sysadmins who work from Linux. DA Toolkit is developed and tested on
Debian/Ubuntu/Mint. It manages Windows machines fine (WinRM and RDP), but it runs on Linux.

Anyone is welcome to port it to Windows or macOS, but I won't be making any effort to do so
or to maintain a port.

## Install

Releases come as a single **AppImage** for x86-64 Linux: download it, make it executable
(`chmod +x DAToolkit-*.AppImage`) and run it. It needs Ubuntu 24.04 / Mint 22, Debian 13, or a
current Fedora, Arch or CachyOS (X11 or Wayland). The app window uses the system's WebKitGTK,
so that it gets your distribution's security updates:

| Distribution | For the app window | For RDP sessions (optional) |
|---|---|---|
| Ubuntu, Mint | `sudo apt install gir1.2-webkit2-4.1` | `sudo apt install guacd` |
| Debian | `sudo apt install gir1.2-webkit2-4.1` | not packaged: guacd container (below) |
| Fedora | `sudo dnf install webkit2gtk4.1` | `sudo dnf install guacd libguac-client-rdp`, then `sudo systemctl enable --now guacd` |
| Arch, CachyOS | `sudo pacman -S webkit2gtk-4.1` | not in the repositories: guacd container (below) |

Where guacd isn't packaged, DA Toolkit runs Apache Guacamole's own image for you: it starts the
container when you open an RDP host and stops it when the last RDP session closes or the app
quits, published on this machine only (guacd's link is unencrypted). Download the image once:

```bash
podman pull docker.io/guacamole/guacd:1.6.0@sha256:8974eaa9ba32f713daf311e7cc8cd7e4cdfba1edea39eed75524e78ef4b08f4f
```

(`docker pull ...` works the same.) Arch's AUR has `guacamole-server`,
but it doesn't build as is: current glibc trips its `-Werror`, and its RDP plugin doesn't
compile against Arch's FreeRDP 3, so it also needs `freerdp2` from the AUR. If guacd isn't running when you open an RDP host, DA Toolkit
shows the commands for your distribution in a dialog you can copy them from.

Without WebKitGTK, DA Toolkit opens in your default web browser instead and says what to install.
You can also choose the browser yourself (Settings → General, or `--browser`); the browser
version has a **Quit** button, because closing the tab doesn't end the app. Every release can
be rebuilt from its commit to the same bytes: see [packaging/README.md](packaging/README.md).

To run from source instead (Python 3.11 or newer):

```bash
sudo apt install python3-venv python3-gi gir1.2-webkit2-4.1   # Debian/Ubuntu/Mint
git clone https://github.com/DigitalArchon/DAToolkit.git && cd DAToolkit
python3 -m venv --system-site-packages .venv   # system-site-packages gives access to GTK/WebKit
.venv/bin/pip install -e .
.venv/bin/datoolkit
```

`--no-open` only prints the local URL, for opening it yourself. Treat that URL like a password:
it grants terminal access.

## Quick start

1. **Start a case**: give it a name or ticket number and pick a sensitivity. To set things up
   first, choose **Not now**: settings, providers, models and hosts don't need a case.
2. **Add a model** under Settings → AI providers. To try DA Toolkit without an API key, add
   the **training provider**: a scripted model that walks through a canned case (the one in
   the screenshot) with nothing leaving the machine.
3. **+ Session** opens a local shell, a saved host (add hosts under Settings → Hosts), or a
   window on your screen, such as a ScreenConnect control window.
4. Describe the problem in the chat, then Run, Skip and Send results as the AI works
   through it.

The [manual](MANUAL.md) covers everything else: the workflow in detail, model tiers and how
attestation works, SSH/WinRM/RDP and window-session specifics, where files are stored and how untrusted output
is handled.

## Roadmap

- Connectors: ConnectWise (push ticket notes and transcripts) and Confluence (pull site
  notes, push runbooks)
- Newer guacd (1.5+) so guacd itself enforces the pinned RDP certificate; RDP session
  recording into the case timeline
- IP/hostname pseudonymisation with local reverse mapping
- Per-client notes library

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report vulnerabilities privately as described
in [SECURITY.md](SECURITY.md), not in public issues.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE). Vendored third-party libraries and their licences
are listed in [src/datoolkit/web/vendor/NOTICE](src/datoolkit/web/vendor/NOTICE).
