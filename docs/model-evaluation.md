# Which models work well with DA Toolkit

*Evaluation of 9 models, October 2026, DA Toolkit 0.1.0b12 → 0.1.0b13*

DA Toolkit works with any OpenAI-compatible model. A good diagnosis isn't enough on its own,
though: the model also has to work inside a gated console. It should put every command it
wants run into the queue, say what it is doing in words, label risk honestly, and remember
the ground rules the technician set an hour ago.

This page describes how nine popular models did at that. It also covers the habits that
tripped them up and what 0.1.0b13 changes because of it. The harness, the scenarios and the
raw numbers are in [`tools/model-eval`](../tools/model-eval), so you can rerun all of it with
your own key.

## Summary

| Model | Solved (first round) | Quality (1–5) | Recommendation |
|---|---|---|---|
| Claude Opus 5.5 | 6/6 | 4.8 | **Recommended.** The strongest diagnostician; the default. |
| Claude Sonnet 5.5 | 6/6 | 4.7 | **Recommended.** Matches Opus at about half the cost. |
| Qwen 3.8 Max | 6/6 | 4.7 | **Recommended.** The best of the rest, and cheap. |
| Kimi K3 | 6/6 | 4.2 | **Good.** Clean tool use; the occasional reasoning slip. |
| GLM 5.3 | 6/6 | 4.2 | **Good with 0.1.0b13.** It needed the new "promise" nudge. |
| Kimi K2.7 Code | 6/6 | 4.5 | **Usable with 0.1.0b13.** It needed the nudge most often. |
| DeepSeek V4 Pro | 4/6 | 3.2 | **Usable with 0.1.0b13** (3/3, quality 4.0 with the changes). Watch that it answers your questions. |
| Qwen 3.7 Plus | 6/6 | 3.8 | **Simple jobs.** It can lock onto a wrong theory. |
| Qwen 3.8 27B | 5/6 | 3.8 | **Borderline.** Included as a stand-in for a model you would run locally. |

Two findings matter more than the ranking:

1. **Context length was not where any model slipped.**
   - Up to about 130,000 tokens of case history, the strong models answered as well as they
     did at 30,000.
   - The failures we saw were habits, and they showed up just as often in short cases.
2. **Most failures were about the tools, not the diagnosis.**
   - Every model found the root cause in nearly every run.
   - What separated them was whether the technician could act on what the model said.

## How it was tested

### Scenarios

The three scenarios each have a hidden cause:

| Scenario | Session | What's really wrong |
|---|---|---|
| Jellyfin playback stutters on high-bitrate files | Linux over SSH | A damaged patch cable: the NIC negotiated 100 Mb/s instead of 1 Gb/s. |
| Users can't log on; "trust relationship" errors | Windows Server 2022 over WinRM | The domain controller's NTP source died with an ISP change. Its clock is 9m40s fast, past Kerberos's 5-minute tolerance. |
| A new guest VLAN gets addresses but no internet | MikroTik RouterOS 7 over SSH | The VLAN interface was never added to the LAN interface list, so the firewall drops its traffic. |

### Running the cases

- **The model under test ran inside the real DA Toolkit engine**, with the same system
  prompt, tools, queue and risk rules as the app. The only differences were stub sessions
  instead of real terminals, and web search declined so every model saw the same evidence.
- **Another AI played the technician and the machines.** It had the scenario's hidden facts
  and invented realistic, consistent command output. It answered questions in character and
  skipped disruptive commands before the cause was clear. It never hinted at the cause.
- **When a model said it would give commands but queued none,** the technician replied the
  way a person would ("Okay... so what are they?").
- **Runs:** two runs per model per scenario, 10 turns at most. The technician scored each
  run 1–5 and noted anything a technician would find odd.

### What was measured automatically, per turn

Every turn was checked automatically for these:

- no message;
- commands announced but not queued;
- commands written only in prose;
- invalid tool calls;
- a risk label lower than DA Toolkit's own rules;
- changes without a rollback;
- shell-rule breaches (pagers, multi-line PowerShell, unbounded `ping`);
- re-asked questions;
- tool-call syntax leaking into the message.

### Context probes

- A consistent synthetic case history was built at about 32k, 64k and 128k tokens:
  - a morning of healthy-looking checks;
  - then an SELinux denial after a policy update.
- Rules were planted in the technician's very first message:
  - no service restarts or reboots before 6pm;
  - the server is RHEL;
  - one session is PowerShell.
- **Three single-turn probes** were sent on top of that history:
  - results that need a next step;
  - "fix it properly and safely";
  - "when can we reboot, and what should we check on the file server first?"
- Each reply was scored on:
  - writing a message and queueing the commands;
  - using the right session and the right OS;
  - keeping the 6pm rule;
  - recalling it when asked;
  - for the fix: the right fix, with a rollback and an honest risk label.

The whole evaluation cost about US$16 through NanoGPT.

## Results

### Scenario runs (first round)

Flag columns are the share of turns showing that behaviour.

| Model | Solved | Quality | Turns/run | Commands announced, none queued | Commands in prose only | Cost (6 runs) |
|---|---|---|---|---|---|---|
| Claude Opus 5.5 | 6/6 | 4.83 | 4.5 | 0% | 33% | $1.21 |
| Claude Sonnet 5.5 | 6/6 | 4.67 | 5.3 | 0% | 44% | $0.61 |
| Qwen 3.8 Max | 6/6 | 4.67 | 6.0 | 0% | 19% | $0.42 |
| Kimi K2.7 Code | 6/6 | 4.50 | 9.2 | 14% | 4% | $0.24 |
| Kimi K3 | 6/6 | 4.17 | 6.5 | 0% | 10% | $0.58 |
| GLM 5.3 | 6/6 | 4.17 | 7.3 | 4% | 11% | $0.15 |
| Qwen 3.7 Plus | 6/6 | 3.83 | 8.2 | 6% | 10% | $0.10 |
| Qwen 3.8 27B | 5/6 | 3.83 | 8.3 | 20% | 14% | $0.07 |
| DeepSeek V4 Pro | 4/6 | 3.17 | 8.8 | 17% | 9% | $0.23 |

Every model found the root cause in every run. The two DeepSeek runs that didn't finish ran
out of turns: three of the ten went on announcing checks it never queued. The Qwen 3.8 27B
failure spent its turns on invalid RouterOS syntax.

### Context probes

Scores are the share of checks passed, out of 1.00.

| Model | ~32k | ~64k | ~128k |
|---|---|---|---|
| Claude Opus 5.5 | 1.00 | 1.00 | 1.00 |
| Kimi K3 | 1.00 | 1.00 | 1.00 |
| GLM 5.3 | 1.00 | 1.00 | 1.00 |
| Qwen 3.8 Max | 0.92 | 0.93 | 1.00 |
| Claude Sonnet 5.5 | 0.93 | 1.00 | 1.00 |
| Qwen 3.7 Plus | 0.96 | 1.00 | 0.89 |
| DeepSeek V4 Pro | 0.87 | 0.96 | 0.96 |
| Kimi K2.7 Code | 1.00 | 0.84 | 0.85 |
| Qwen 3.8 27B | 0.84 | 0.69 | 0.85 |

Scores don't fall as the history grows. The misses are the same habits as in the scenario
runs:
- no message (Sonnet at 32k);
- a fix described but not queued (Kimi K2.7 Code, both Qwen 3.8 models);
- DeepSeek putting off the "when can we reboot" question three times out of three instead
  of answering it with the 6pm rule.

Each cell is three single replies, so read these as indications rather than precise
measurements.

## What tripped the models up, and what changed

### 1. "Now I need you to run a few more commands" — with no commands

**What happened.** Some models end a turn by announcing checks they never queue. The
technician is left with an empty queue and has to ask what to run. Examples from the runs:

- **GLM 5.3:** "Let's get a baseline first with some read-only checks on media01."
- **DeepSeek V4 Pro:** "I'll queue a few read-only checks to understand the hardware, load,
  and how the NAS share is mounted."
- **Kimi K2.7 Code:** "No service restart is required. I'll also make it persistent so it
  survives a reboot."
- **Qwen 3.8 27B:** "That said, since you asked, I've queued two cheap read-only checks."
  (It hadn't.)
- **Qwen 3.8 Max:** "The fix is to turn on `httpd_can_network_connect`." (No call.)

GLM 5.3 also asked the technician to "run #10 and #11" after those items had been skipped
and had left the queue.

**What changed.**
- **The promise nudge.** When a reply announces commands, a fix or checks and queues nothing,
  DA Toolkit asks the model once to queue them, or to say plainly that nothing is needed.
  - The check reads each sentence of the reply's last lines.
  - It ignores sentences about items already in the queue, and offers that wait on you
    ("If you'd like, I'll propose a rule…").
  - Its patterns come from the phrasings seen in these runs. It flagged none of the 155
    turns from the four models that never made empty promises (Opus, Sonnet, Kimi K3,
    Qwen 3.8 Max).
- **The stale-reference nudge.** Asking you to run an item that is no longer pending gets a
  note naming the item's status.
- **Built-in notes.** GLM, DeepSeek, Kimi K2.x and the smaller Qwen models get a short rule
  about queueing in the same turn. DeepSeek's note also asks it to answer every question
  before moving on.

**The effect** (share of turns that still ended with an empty promise):

| Model | Before | With 0.1.0b13 |
|---|---|---|
| Qwen 3.8 27B | 20% | 0% |
| DeepSeek V4 Pro | 17% | 7% |
| Kimi K2.7 Code | 14% | 5% |
| Qwen 3.7 Plus | 6% | 3% |
| GLM 5.3 | 4% | 6% |

The remaining cases used phrasings the detector learned during this work, such as "The fix:
…" and "Let me check X and see if…". The released version catches them. GLM's percentages
come from very few turns: one empty promise in 18. DeepSeek with the changes solved 3 of 3,
against 4 of 6 before.

### 2. Commands with no word about them

**What happened.** Claude models reason before acting. With reasoning on, they often go
straight from reasoning to tool calls, and the technician sees commands appear with no
explanation. DA Toolkit already asked once for the missing message; that nudge fired in 26%
of Opus turns and 38% of Sonnet turns.

The evaluation found two holes in it:

- **At 64k tokens (Opus) and 32k (Sonnet),** the model answered the nudge with more tool
  calls (sometimes different commands) and still no text.
- **Qwen 3.8 Max answered with nothing at all** once tools were withheld.

**What changed.**
- The nudge round now asks for words only (`tool_choice: none`), and every model tested
  accepts that.
- If that round comes back empty, it is asked once more with the tools back.

After the change, Sonnet had no message-less turns in its scenario runs or its probes.

### 3. A turn that stops halfway

**What happened.** Kimi K2.7 Code planned, in its reasoning, to "update the hypotheses and
propose commands". It made the first call and stopped. Its message ended mid-sentence, "Let's
inspect the relevant config on", and nothing was queued.

**What changed.** A hypothesis-board update takes effect immediately, so a round whose only
call was that update no longer ends the turn when its message:
- is cut off,
- is empty, or
- promises more.

The model gets another round to finish.

### 4. Tool calls written as text

**What happened.** Smaller and local models sometimes print their tool call into the message
instead of making it. Qwen 3.8 27B did this:

```
<tool_call>
<function=propose_commands>
<parameter=items>
[{"session_id": "app01", "command": "sudo setsebool -P httpd_can_network_connect on", ...}]
```

The technician saw markup and nothing was queued.

**What changed.** These are now read back into ordinary tool calls, and the markup is
removed from the message. The calls then go through the same queue, risk rules and review as
any other. The formats handled are:
- Qwen-style XML;
- Hermes-style JSON in `<tool_call>` tags;
- DeepSeek's DSML.

DeepSeek leaked DSML once in a quick check of the `tool_choice` setting, though never in the
runs themselves.

### 5. Placeholders in queued commands

**What happened.**
- Opus queued `dd if='/mnt/nas/PATH/TO/stuttering-file.mkv' …`.
- Kimi K2.7 Code queued `ping -c 20 <NAS_IP>`, and bash read `<NAS_IP>` as a redirect.

**What changed.**
- The system prompt now says never to queue a placeholder: find the value first, or ask.
- If a model does it anyway, the reply to its tool call names the item and asks for it to be
  withdrawn and queued properly. You should still look closely at such an item before
  running it.

### 6. Checks after a fix, backups and safety nets

**What happened.** Every model left verification steps, backups and rollbacks in its message
text instead of the queue. Opus and Sonnet did this in a third or more of their turns.
Not one model, in any run, suggested RouterOS Safe Mode before changing firewall rules over
the SSH session it was using.

**What changed.** The system prompt now asks for:
- checks after a fix, backups and anything "for later" to be queued (or queued once the
  results are in);
- a backup and the device's safety net before network-device changes over the connection in
  use: Safe Mode on RouterOS, `commit confirmed` on Junos and VyOS, `reload in` on Cisco IOS.

Commands for a machine with no open session can still be given in the message, with a word
about which machine they are for. In the runs with the changes, Sonnet used Safe Mode and a
config backup for its follow-up router changes.

### 7. Risk labels on quoted text

**What happened.** DA Toolkit's local risk rules read the whole command text. They took a `>`
inside an awk program for an output redirect. So this read-only bitrate check was labelled
*modifying*:

```bash
ffprobe … | awk -F, 'NR>1{…} END{for(i=0;i<=b;i++) printf …}'
```

The evaluation found more false alarms of the same kind, each raised a level:
- `mount -t cifs` (which only lists mounts);
- `net share` with no arguments;
- `dd … of=/dev/null` (a read test, labelled disruptive);
- the `iptables -S`, `nft list` and `New-Object` steps in DA Toolkit's own recipes.

**What changed.** Quoted text is data, unless the command hands it back to a shell (`sh -c`,
`ssh`, `xargs`, `find -exec` and similar). The listing forms of those commands are read only.

The rules still only ever raise a label:
- an awk program that writes a file or runs a command (`print > "file"`, `system()`) is still
  *modifying*;
- the changing forms of every command above still raise.

Replaying the rules over earlier cases changed no past label, apart from the false alarms.

### 8. Your own instructions per model

Settings → Model → **Model-specific instructions** lets you add text to the end of the
system prompt for any model whose id contains a match you choose. The settings show DA
Toolkit's built-in notes, and you can turn them off. If a model you use has a habit that
annoys you, a sentence there is usually enough.

## Per model

**Claude Opus 5.5**
- Methodical, quick to the cause (turn 2–3), and careful with changes: backups, rollbacks,
  config tests before reloads, no forcing link speed over its own SSH session.
- In one run it caught an implausible throughput number on its own. In another it refused a
  "fix" that the planted evidence didn't support.
- Weak spots, both addressed in 0.1.0b13:
  - message-less turns;
  - verification commands left in prose.

**Claude Sonnet 5.5**
- Essentially as good as Opus, a little faster and about half the price.
- It had the most message-less turns before the fix, and none after it.

**Qwen 3.8 Max**
- The surprise of the set:
  - 6/6 solved, quality 4.7;
  - no empty promises;
  - efficient on RouterOS;
  - at roughly a third of Sonnet's cost.
- It sometimes guesses a file path, or uses a `dd` flag that CIFS rejects.

**Kimi K3**
- Clean tool use, with no empty promises.
- The weak spots are occasional reasoning slips:
  - a firewall rule that could never match;
  - a ping sent from the router itself, offered as proof that forwarding works;
  - `Test-ComputerSecureChannel` run on the DC, which tests only the DC itself.

**GLM 5.3**
- Correct diagnoses, and it made the narrower choice for the RouterOS fix.
- It occasionally ends a turn with "Let's get a baseline first…" and nothing queued. This is
  the behaviour that started this evaluation, and the nudge and note now handle it.

**Kimi K2.7 Code**
- Good diagnoses.
- It announced checks without queueing them more than any other model (14% of turns). It
  sometimes stops mid-sentence, and once queued a placeholder.
- Much better with 0.1.0b13.

**DeepSeek V4 Pro**
- Finds causes, but wastes turns:
  - announcing checks it doesn't queue;
  - putting off direct questions;
  - once getting the direction of the clock skew backwards.
- With 0.1.0b13 it solved all three of its runs.

**Qwen 3.7 Plus**
- Fine on clear-cut problems.
- In one run it fixated on hardware transcoding, although its own evidence showed Direct
  Play, and it never found the cable.

**Qwen 3.8 27B**
- This size of model is what people run locally. It found every root cause, but it struggles
  with device syntax (RouterOS).
- Once its reasoning leaked into the message and ran away into a list of numbers.
- 0.1.0b13 helps: no empty promises in its runs with the changes, and tool calls written as
  text are recovered.
- For client work, prefer a larger model where the case's sensitivity allows it.

## Limits of this evaluation

- **Small samples.** Six runs per model and three with the changes. One reply per probe
  cell.
- **AI-played technician.** The technician and machines were played by Claude, and the
  quality scores are its judgement. That may favour Claude-like behaviour. The automatic
  per-turn checks don't depend on that judgement.
- **NanoGPT routes.** Every model was used through NanoGPT, with DA Toolkit's default
  generation settings (temperature 0.3, reasoning effort low). Other providers, quantised
  local builds or higher reasoning effort may behave differently.
- **Context.** Context beyond about 130k tokens wasn't tested. Neither were images,
  research, or the end-to-end encrypted and TEE routes. The GLM, Kimi and Qwen notes match
  those routes too (e.g. `private/glm-5-3`, `TEE/qwen3.8-27b`).
- **One harness flaw, fixed during the work.** Early runs sent results stamped with the test
  machine's clock, which disagreed with the times in the invented logs. One model rightly
  pointed out the gap and was marked down for it; that score is corrected above.

## Rerunning it

```bash
# one scenario run, played turn by turn (see tools/model-eval/HOST_BRIEF.md)
.venv/bin/python tools/model-eval/harness.py start --model z-ai/glm-5.3 --scenario dc-kerberos --run-id my-run
# context probes
.venv/bin/python tools/model-eval/probe.py --model z-ai/glm-5.3 --sizes 32000 64000 128000
# tables
.venv/bin/python tools/model-eval/report.py
```

The harness reads your NanoGPT key from the keyring entry DA Toolkit uses, opens no ports,
and stops at a spending limit (`EVAL_BUDGET_USD`, default US$30).
