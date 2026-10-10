# Model evaluation findings (2026-10-10)

The tables are in `summary.md`. Each turn's record is under `runs/`, which is not committed.

## How it was measured

- **9 models, 3 scenarios, 2 runs each (54 runs).** The scenarios are:
  - Jellyfin stutter on Linux over SSH, caused by a 100 Mb/s link from a bad cable.
  - DC clock skew over WinRM, caused by a dead NTP peer.
  - A RouterOS guest VLAN missing from an interface list.
- **The technician and the machines were played by Claude agents** working from each scenario's hidden facts. Nothing ran anywhere.
- **"After" runs:** 1 run per scenario, on the `model-prompts` branch (nudges, model notes, prompt rules). These covered the models that needed them, plus Sonnet to check the no-message fix.
- **Context probes:** one reply each on top of a consistent synthetic case at ~32k, ~64k and ~128k tokens. Each probe checks:
  - a follow-up after results;
  - a change request;
  - recall of rules set in the first message (no restarts before 6pm, RHEL, PowerShell).
- **Spend:** US$16.20 in total. The Opus 128k probes were the largest single items.

## Verdict per model

| Model | Base: solved / quality | After | Use it? |
|---|---|---|---|
| Claude Opus 5.5 | 6/6, 4.83 | (probes 1.00) | **Yes: the default.** Best diagnosis; the no-message bug is fixed. |
| Claude Sonnet 5.5 | 6/6, 4.67 | 3/3, 5.0 | **Yes.** Same as Opus at about half the cost. |
| Qwen 3.8 Max | 6/6, 4.67 | (probes 1.00) | **Yes.** The best non-Claude model: no empty promises, cheap. |
| Kimi K3 | 6/6, 4.17 | - | **Yes.** Clean tool use; the occasional reasoning slip. |
| GLM 5.3 | 6/6, 4.17 | 3/3, 4.67 | **Yes, with the nudge/note.** Your "so what are they?" behaviour was reproduced and is now handled. |
| Kimi K2.7 Code | 6/6, 4.50 | 3/3, 4.67 | **Usable with the nudge/note.** It sometimes cuts its message off mid-sentence. |
| DeepSeek V4 Pro | 4/6, 3.17 | 3/3, 4.0 | **Usable with the nudge/note.** It sidesteps questions and ground rules, and once got the skew direction backwards. |
| Qwen 3.7 Plus | 6/6, 3.83 | 2/3, 3.67 | **Simple work only.** It can fixate on a wrong theory. |
| Qwen 3.8 27B (stand-in for a local model) | 5/6, 3.83 | 3/3, 3.67 | **Borderline.** RouterOS syntax trouble, and one runaway reply. |

## Where they slip

- **Context length is not where these models slip, up to ~130k tokens.**
  - Opus, Sonnet (after the fix), Kimi K3, GLM 5.3 and Qwen 3.8 Max scored 1.00 at every size.
  - The failures were habits, and they appeared as much at 32k as at 128k.
  - Each probe is a single reply, so treat the per-size numbers as indicative.
- **The main habit is announcing commands without queueing them** ("I'll queue a few read-only checks", "The fix is to…", "Let's get a baseline first"). Share of turns, before → after:

  | Model | Before | After |
  |---|---|---|
  | Qwen 3.8 27B | 20% | 0% |
  | DeepSeek | 17% | 7% |
  | Kimi K2.7 Code | 14% | 5% |
  | Qwen 3.7 Plus | 6% | 3% |
  | GLM | 4% | 6% |

  - Base runs: 54 turns. After runs: 18–27 turns per model.
  - **The "after" turns still counted are phrasings the detector learned during this work.** With the final patterns, the engine nudges them.
  - DeepSeek lost two base runs to the 10-turn limit this way.
  - Opus, Sonnet, Kimi K3 and Qwen 3.8 Max never did it.
- **Claude's habit is the opposite: tool calls with no message.** It came from reasoning going straight to the tools: 26% of Opus turns and 38% of Sonnet turns needed the nudge. At 64k (Opus) and 32k (Sonnet), the old nudge drew more tool calls and still no text. The nudge round now uses `tool_choice: none`. Afterwards, Sonnet had no message-less turns in its runs or probes.
- **Every model left verification, backup and rollback commands in prose**, and none mentioned Safe Mode before RouterOS edits. The system prompt now asks for these explicitly. After that change, Sonnet used Safe Mode and a backup for its later RouterOS edits.

## What changed because of it

- **Classifier (branch `fix-quoted-redirect`):**
  - A `>` or `;` inside quotes is no longer read as shell syntax, so your ffprobe | awk command is read only.
  - The listing forms of `mount`, `iptables -S`/`nft list`, `net share`/`net user`, `dd of=/dev/null`, `New-Object` and `Start-Sleep` are read only.
  - Five built-in recipe steps were mis-rated and now match their declared risk.
- **Engine (branch `model-prompts`):**
  - The no-message nudge offers no tools. An empty answer to it is asked again with the tools back.
  - A promise nudge, plus a stale-reference nudge for "run #10" when #10 was skipped.
  - A turn carries on after a board-only round whose message is cut off, promises more, or is empty.
  - Tool calls written as text (Qwen XML, Hermes JSON, DeepSeek DSML) are read back as real calls.
  - Placeholders in queued commands (`<NAS_IP>`, `PATH/TO`) are sent back to the AI.
- **Prompts:**
  - Queue follow-up checks and backups.
  - No placeholders.
  - Use a safety net before network-device changes (Safe Mode, `commit confirmed`, `reload in`).
  - Built-in notes for GLM, DeepSeek, Kimi K2.x and Qwen (not Qwen Max).
  - Your own notes per model, in Settings → Model.

## Caveats

- **Small samples:** 6 base runs and 3 after runs per model, with a single run per probe. Quality is the host agent's judgement.
- **The hosts are Claude**, so their judgement may favour Claude-like behaviour.
- **One harness artefact, fixed during the work.** The Opus, Sonnet and GLM base runs sent results stamped with this machine's real start times, which contradicted the hosts' invented logs. GLM rightly pointed out a 2.5-hour "gap" that came from the harness, and the GLM `dc-kerberos` r2 host docked it for this. Read that run as 4/5, not 3/5.
- **Nudges cost one extra round when they fire**, and they fire only on turns that queued nothing.
