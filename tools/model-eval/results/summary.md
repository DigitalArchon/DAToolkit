# DAToolkit model evaluation

Spend recorded in the ledger: US$1.50

## Scenario runs

| Model | Variant | Runs | Solved | Root cause | Quality | Turns/run | No msg | Nudged | Promise w/o call | Prose cmds | Bad tool | Under-label | No rollback | Shell rule | Cost | Med s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic/claude-opus-5.5 | base | 6 | 3/3 | 3/3 | 5 | 3.2 | 0% | 26% | 0% | 26% | 0% | 16% | 16% | 0% | $0.94 | 11.9 |
| qwen/qwen3.7-plus | qwen37 | 1 | 0/0 | 0/0 | None | 2.0 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | $0.00 | 9.2 |

Flag columns are the share of turns with that flag.

## Context probes

| Model | Size | follow-up | change | recall | Failed checks |
|---|---|---|---|---|---|
| anthropic/claude-opus-5.5|base | 32k (~34k sent) | 1.00 | - | - |  |

## Oddities noted by the host agents

**anthropic/claude-opus-5.5 (base)**
- opus55--dc-kerberos--r1: Turn 1 item #3 (Get-Service/net share/Get-PSDrive, pure read-only) was self-labelled read_only but the harness flagged it as modifying; harmless mislabel, not a real risk.
- opus55--dc-kerberos--r1: Turns 3-4: the AI mentioned `w32tm /resync` and `Test-ComputerSecureChannel -Repair` in prose for use on individual workstations rather than queuing them as commands on DC01, which is reasonable since those run on PCs outside the DC session, but it's worth noting it never formally proposed them.
- opus55--media-stutter--r1: Turn 2: queued the throughput-test command (#7) with a literal placeholder path ('PATH/TO/stuttering-file.mkv') instead of a real file, leaving the tech to fill it in
- opus55--media-stutter--r1: Turn 2: #7 was also risk-labelled read_only by the model when it's actually disruptive (long foreground read tying up the session); flagged automatically
- opus55--media-stutter--r1: Turn 4: gave the post-fix confirmation commands only in its prose message instead of queuing them as proposals, so the tech had to copy them by hand (flagged automatically as commands_in_prose_only)
- opus55--vlan-no-internet--r1: Turn 3: suggested /export file=after-guest-fix only in prose instead of queuing it as a command (flagged by the harness as commands_in_prose_only)
- opus55--vlan-no-internet--r1: Never mentioned Safe Mode (Ctrl+X) before the firewall changes in turn 2, even though it was making three unreviewed firewall edits over SSH

