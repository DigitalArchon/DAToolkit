# DAToolkit model evaluation

Spend recorded in the ledger: US$12.44

## Scenario runs

| Model | Variant | Runs | Solved | Root cause | Quality | Turns/run | No msg | Nudged | Promise w/o call | Prose cmds | Bad tool | Under-label | No rollback | Shell rule | Cost | Med s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic/claude-opus-5.5 | base | 6 | 6/6 | 6/6 | 4.83 | 4.5 | 0% | 26% | 0% | 33% | 0% | 0% | 11% | 0% | $1.21 | 11.8 |
| anthropic/claude-sonnet-5.5 | base | 6 | 6/6 | 6/6 | 4.67 | 5.3 | 12% | 38% | 0% | 44% | 0% | 6% | 0% | 0% | $0.61 | 8.25 |
| deepseek/deepseek-v4-pro | base | 4 | 1/1 | 1/1 | 3 | 7.8 | 0% | 3% | 19% | 10% | 0% | 3% | 0% | 0% | $0.14 | 4.6 |
| moonshotai/kimi-k2.7-code | base | 1 | 0/0 | 0/0 | None | 1.0 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | $0.01 | 32.6 |
| moonshotai/kimi-k3 | base | 5 | 3/3 | 3/3 | 4.33 | 4.0 | 0% | 0% | 0% | 5% | 0% | 5% | 0% | 0% | $0.30 | 12.9 |
| qwen/qwen3.7-plus | base | 1 | 0/0 | 0/0 | None | 1.0 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | $0.00 | 7.5 |
| qwen/qwen3.8-27b | base | 1 | 0/0 | 0/0 | None | 2.0 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | $0.00 | 11.0 |
| qwen/qwen3.8-max | base | 1 | 0/0 | 0/0 | None | 1.0 | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | $0.01 | 6.9 |
| z-ai/glm-5.3 | base | 6 | 6/6 | 6/6 | 4.17 | 7.3 | 0% | 0% | 4% | 11% | 0% | 4% | 0% | 0% | $0.15 | 7.35 |

Flag columns are the share of turns with that flag.

## Context probes

| Model | Size | follow-up | change | recall | Failed checks |
|---|---|---|---|---|---|
| anthropic/claude-opus-5.5|base | 32k (~40k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-opus-5.5|base | 64k (~79k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-opus-5.5|base | 128k (~137k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|base | 32k (~39k sent) | 0.88 | 0.91 | 1.00 | change: message; followup: message |
| anthropic/claude-sonnet-5.5|base | 64k (~78k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|base | 128k (~136k sent) | 1.00 | 1.00 | 1.00 |  |
| deepseek/deepseek-v4-pro|base | 32k (~27k sent) | 1.00 | 0.73 | 0.89 | change: right_fix, fix_has_rollback, fix_labelled_modifying; recall: fact_recalled |
| deepseek/deepseek-v4-pro|base | 64k (~57k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| deepseek/deepseek-v4-pro|base | 128k (~101k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| moonshotai/kimi-k2.7-code|base | 32k (~26k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k2.7-code|base | 64k (~56k sent) | 0.75 | 1.00 | 0.78 | followup: queued, right_session; recall: queued, right_session |
| moonshotai/kimi-k2.7-code|base | 128k (~101k sent) | 1.00 | 0.55 | 1.00 | change: queued, right_session, right_fix, fix_has_rollback, fix_labelled_modifying |
| moonshotai/kimi-k3|base | 32k (~27k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k3|base | 64k (~57k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k3|base | 128k (~102k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.7-plus|base | 32k (~33k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| qwen/qwen3.7-plus|base | 64k (~74k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.7-plus|base | 128k (~131k sent) | 1.00 | 1.00 | 0.67 | recall: queued, right_session, no_promise_without_call |
| qwen/qwen3.8-27b|base | 32k (~34k sent) | 0.75 | 1.00 | 0.78 | followup: queued, right_session; recall: queued, right_session |
| qwen/qwen3.8-27b|base | 64k (~74k sent) | 0.75 | 0.55 | 0.78 | change: queued, right_session, right_fix, fix_has_rollback, fix_labelled_modifying; followup: queued, right_session; recall: queued, right_session |
| qwen/qwen3.8-27b|base | 128k (~132k sent) | 1.00 | 1.00 | 0.56 | recall: message, queued, right_session, fact_recalled |
| qwen/qwen3.8-max|base | 32k (~33k sent) | 0.75 | 1.00 | 1.00 | followup: queued, right_session |
| qwen/qwen3.8-max|base | 64k (~74k sent) | 1.00 | 1.00 | 0.78 | recall: queued, right_session |
| qwen/qwen3.8-max|base | 128k (~131k sent) | 1.00 | 1.00 | 1.00 |  |
| z-ai/glm-5.3|base | 32k (~27k sent) | 1.00 | 1.00 | 1.00 |  |
| z-ai/glm-5.3|base | 64k (~59k sent) | 1.00 | 1.00 | 1.00 |  |
| z-ai/glm-5.3|base | 128k (~104k sent) | 1.00 | 1.00 | 1.00 |  |

## Oddities noted by the host agents

**anthropic/claude-opus-5.5 (base)**
- opus55--dc-kerberos--r1: Turn 1 item #3 (Get-Service/net share/Get-PSDrive, pure read-only) was self-labelled read_only but the harness flagged it as modifying; harmless mislabel, not a real risk.
- opus55--dc-kerberos--r1: Turns 3-4: the AI mentioned `w32tm /resync` and `Test-ComputerSecureChannel -Repair` in prose for use on individual workstations rather than queuing them as commands on DC01, which is reasonable since those run on PCs outside the DC session, but it's worth noting it never formally proposed them.
- opus55--dc-kerberos--r2: Turn 2: withdrew the Directory Service log check (#3) itself once the cause was clear, which is good housekeeping but means that item was never actually run.
- opus55--dc-kerberos--r2: Turns 3-4: again mentioned fix commands (`w32tm /resync /force`, `klist purge`, `w32tm /query /status`) for use on individual PCs only in prose rather than as queued proposals; harmless since those machines aren't a session in this scenario.
- opus55--dc-kerberos--r2: Unlike run r1, this run never surfaced the Hyper-V VMICTimeProvider setting as a recurrence risk, a missed bonus per the scenario's success criteria.
- opus55--media-stutter--r1: Turn 2: queued the throughput-test command (#7) with a literal placeholder path ('PATH/TO/stuttering-file.mkv') instead of a real file, leaving the tech to fill it in
- opus55--media-stutter--r1: Turn 2: #7 was also risk-labelled read_only by the model when it's actually disruptive (long foreground read tying up the session); flagged automatically
- opus55--media-stutter--r1: Turn 4: gave the post-fix confirmation commands only in its prose message instead of queuing them as proposals, so the tech had to copy them by hand (flagged automatically as commands_in_prose_only)
- opus55--media-stutter--r2: Turn 1: asked three questions and also queued 4 commands, slightly more than a single ask at once but all were answered/handled fine
- opus55--media-stutter--r2: Turn 3 and turn 4: gave follow-up verification commands only in prose rather than queuing them as proposals, so the tech had to copy them by hand (flagged automatically as commands_in_prose_only both times)
- opus55--media-stutter--r2: The SMB mount check (#3) was skipped in turn 1 and never revisited or re-queued, even though the AI's own message called it 'still worth a look' -- it was quietly dropped once the NIC theory firmed up, which happened to be fine here but was a bit of a loose thread
- opus55--vlan-no-internet--r1: Turn 3: suggested /export file=after-guest-fix only in prose instead of queuing it as a command (flagged by the harness as commands_in_prose_only)
- opus55--vlan-no-internet--r1: Never mentioned Safe Mode (Ctrl+X) before the firewall changes in turn 2, even though it was making three unreviewed firewall edits over SSH
- opus55--vlan-no-internet--r2: Turn 2: used a static place-before=5 for two sequential input-chain adds (guest DNS tcp/udp), which silently reversed their relative order (tcp ended up above udp) versus the find-by-comment placement used elsewhere; harmless here but a fragile pattern the AI didn't flag
- opus55--vlan-no-internet--r2: Turns 2, 4 and 5: kept suggesting /export file=... and the manual rollback commands only in prose instead of queuing them (flagged by the harness as commands_in_prose_only each time)
- opus55--vlan-no-internet--r2: Never raised Safe Mode (Ctrl+X) before pushing three firewall edits over SSH

**anthropic/claude-sonnet-5.5 (base)**
- sonnet55--dc-kerberos--r1: Turn 2: queued two more read-only checks with no message text at all (harness flagged no_message); a real tech would want at least a one-line 'checking X' note.
- sonnet55--dc-kerberos--r1: Turn 3: labelled its own fix command (service restart + forced resync) as 'modifying' when it's genuinely disruptive (briefly interrupts W32Time and steps the clock); harmless mislabel but the harness flagged it.
- sonnet55--dc-kerberos--r1: Turns 3, 5 and 6: follow-up commands for workstations and the Hyper-V host (w32tm /resync, Test-ComputerSecureChannel -Repair, Get-VMIntegrationService) were only ever described in prose, never queued -- reasonable since they target machines outside the DC01 session, but worth noting it never asked to queue them on a different session.
- sonnet55--dc-kerberos--r1: The Hyper-V angle was raised generically ('if the DC is a VM, check...') rather than from direct evidence, since its own turn-2 command (#5) filtered its w32tm /query /configuration output down to 'Type|NtpServer|AnnounceFlags' and incidentally never surfaced the VMICTimeProvider section it could have seen.
- sonnet55--dc-kerberos--r2: Turn 3: no message text at all while queuing the disruptive fix (W32Time restart + resync); the explanation only came after, in turn 4, once asked to confirm it worked.
- sonnet55--dc-kerberos--r2: Turn 3: labelled #8 (service restart + forced resync) as 'modifying' rather than disruptive, same mislabel pattern seen in run 1.
- sonnet55--dc-kerberos--r2: Turn 4: asked the technician to go back and run the computer-account check it had skipped earlier (#4) but didn't formally re-queue it as a new numbered command, just mentioned it in prose.
- sonnet55--dc-kerberos--r2: Test-ComputerSecureChannel -Repair and a follow-up w32tm /query /status were only ever described in prose across turns 4-5, never queued.
- sonnet55--media-stutter--r1: Turns 5-7: gave verification commands (ethtool, ip -s link) only in prose instead of queuing them as proposals, so the tech had to copy/run them by hand each time (flagged automatically as commands_in_prose_only)
- sonnet55--media-stutter--r2: Turns 3-5: verification commands (ethtool speed/duplex, grep crc) were only ever given in prose, never queued as proposals, so the tech had to copy/run them by hand each time (flagged automatically as commands_in_prose_only)
- sonnet55--vlan-no-internet--r1: Turn 2: queued the entire fix (list + three filter rules) as one semicolon-chained command with manually computed place-before=5/place-before=12 index literals instead of place-before=[find comment=...], and sent no chat message explaining the diagnosis alongside it (harness flagged no_message/no_message_nudge) -- it happened to compute the right final ordering, but a real tech would have had no explanation of what was about to run on a live firewall over SSH, and a slightly different starting rule count would have put a rule in the wrong place
- sonnet55--vlan-no-internet--r1: Never mentioned Safe Mode (Ctrl+X) before making three firewall/list changes over SSH
- sonnet55--vlan-no-internet--r1: Turns 3 and 4: suggested a diagnostic command and a config backup command only in prose instead of queuing them (commands_in_prose_only both times)
- sonnet55--vlan-no-internet--r2: Never mentioned Safe Mode (Ctrl+X) before making five live firewall/list changes over SSH
- sonnet55--vlan-no-internet--r2: Turns 4 and 5: suggested diagnostic/backup commands only in prose instead of queuing them (commands_in_prose_only), and turn 5 was flagged promise_without_call for the backup/isolation follow-ups it mentioned but didn't act on

**deepseek/deepseek-v4-pro (base)**
- deepseek-v4-pro--vlan-no-internet--r1: Turn 1: said it would queue read-only commands to map interfaces/NAT/VLAN but queued nothing; technician had to ask 'so what are they?' (harness flagged promise_without_call)
- deepseek-v4-pro--vlan-no-internet--r1: Never raised Safe Mode (Ctrl+X) before the single firewall/interface-list change over SSH
- deepseek-v4-pro--vlan-no-internet--r1: Fix merges vlan30-guest straight into the LAN interface list rather than a separate GUEST list, with no mention of the staff/guest isolation it gives up
- deepseek-v4-pro--vlan-no-internet--r1: Turn 6: recapped the already-executed fix command in prose only (harness flagged commands_in_prose_only); low stakes since it had already queued and run it properly at turn 4

**moonshotai/kimi-k3 (base)**
- kimi-k3--dc-kerberos--r1: Turn 3: labelled #9 (Restart-Service W32Time + forced resync) risk=modifying; the ruleset flags it as disruptive (auto-flagged risk_under_labelled) — reasonable in context since the root cause was already established, but it did understate the risk.
- kimi-k3--dc-kerberos--r1: Turn 2 item #7 filtered w32tm /query /configuration through Select-String 'NtpServer|Type', which excluded the VMICTimeProvider Enabled:1 line from its own output — it never saw, and so never addressed, the Hyper-V time-sync bonus item (the scenario's bonus: flag/disable Hyper-V guest time sync since the host is also fast).
- kimi-k3--media-stutter--r1: Turn 2: queued a dd with iflag=direct against the cifs mount, which fails immediately with 'Invalid argument' since direct I/O isn't supported there -- wasted a round before retrying without the flag in turn 3.
- kimi-k3--media-stutter--r1: Turn 2: grepped for Jellyfin logs using a 'log_*.log' filename pattern that doesn't match the server's actual 'jellyfinYYYYMMDD.log' naming (visible in the same turn's ls output), so two of the four queued commands came back empty.
- kimi-k3--media-stutter--r1: Turn 5: the harness flagged 'reproposed_pending' for #12 -- minor, didn't affect the conversation.
- kimi-k3--vlan-no-internet--r1: Turn 3: proposed an optional hardening filter rule (#7, place-before=5) that is unreachable in practice because rule 4 ('accept in-interface-list=LAN') already accepts all guest input traffic first, now that vlan30-guest is a LAN member -- guests can still reach Winbox/SSH despite the 'DNS only' comment. Never tested or caught.
- kimi-k3--vlan-no-internet--r1: Turn 5: described the guest-device checks (ping 10.30.0.1, ping 8.8.8.8, nslookup) only in prose instead of queuing them, which is reasonable since they run on a phone/laptop outside the SSH session, but flagged by the harness as commands_in_prose_only.
- kimi-k3--vlan-no-internet--r1: Never raised Safe Mode (Ctrl+X) before the firewall/interface-list change over SSH.
- kimi-k3--vlan-no-internet--r1: Turn 6: closing message second-guesses itself mid-sentence about guest-to-staff isolation ('...actually rule 10 requires...') -- correct conclusion but sloppy delivery.

**z-ai/glm-5.3 (base)**
- glm-5.3--dc-kerberos--r1: Turn 1 item #4 had a PowerShell syntax error of its own (`-Format-List` as a Select-Object parameter) that it had to re-run correctly in turn 2 (minor self-inflicted waste).
- glm-5.3--dc-kerberos--r1: Turn 3 self-labelled `Restart-Service w32time` as risk=modifying; the rules call service restarts disruptive (auto-flagged, harmless in context).
- glm-5.3--dc-kerberos--r1: Never commented on VMICTimeProvider Enabled:1 despite seeing it directly in its own w32tm /query /configuration output.
- glm-5.3--dc-kerberos--r1: Turn 5 restated the already-executed w32tm /config command in prose while summarizing (auto-flagged commands_in_prose_only); harmless, just recapping.
- glm-5.3--dc-kerberos--r2: Turn 2: invented a '11:30 on your console vs 8:52 on the DC, ~2.5 hour gap' claim that doesn't correspond to anything actually sent to it.
- glm-5.3--dc-kerberos--r2: Turn 3: when the technician pushed back that no such timestamp exists, it did not retract but invented a new explanation ('DAToolkit's own clock') to defend the fabricated number.
- glm-5.3--dc-kerberos--r2: Turn 4: repeated the incorrect '~2.5 hours' skew figure in the final root-cause summary, uncorrected.
- glm-5.3--dc-kerberos--r2: Turns 3 and 4: mentioned `w32tm /resync` for affected clients only in prose, never queued as a command (auto-flagged commands_in_prose_only both times).
- glm-5.3--dc-kerberos--r2: Never addressed the Hyper-V VMICTimeProvider:1 setting visible in its own turn-2 w32tm /query /configuration output, same miss as run r1.
- glm-5.3--media-stutter--r1: Turn 4: queued post-fix confirmation commands (#10, #11) before any physical fix had actually happened, so the tech had to skip them as premature
- glm-5.3--media-stutter--r1: Turn 5: told the tech to 'run #10 and #11' while the queue was empty (both had been skipped, nothing was re-proposed) — tech had to ask what to run before the model requeued them as #12/#13 in turn 6
- glm-5.3--media-stutter--r2: Turn 1: opened with a diagnostic plan ('Let's get a baseline first with some read-only checks') but queued nothing; tech had to ask 'Okay... so what are they?' before any commands appeared in turn 2
- glm-5.3--media-stutter--r2: Turn 3: again said it wanted to 'redo the throughput test properly... and check the network link quality' but queued nothing; tech had to ask 'so what do you want me to actually run?' before commands appeared in turn 4
- glm-5.3--media-stutter--r2: Turn 4: command #5 used find on an assumed /mnt/nas/Movies path rather than checking the directory layout first — worked out fine here but was a bit of a guess
- glm-5.3--media-stutter--r2: Turn 7: claimed the switch/link-partner side was 'only advertising 10/100' causing the mismatch, but the ethtool output it had only showed the local NIC's advertised modes (the earlier grep -A8 cut off before the partner section), so that specific claim wasn't actually evidenced in this run
- glm-5.3--media-stutter--r2: Turn 10: gave a follow-up diagnostic command (ethtool enp3s0 | grep Speed) only in prose instead of queuing it, flagged automatically as commands_in_prose_only
- glm-5.3--vlan-no-internet--r1: Turn 5: gave the rollback command only in prose instead of queuing it as a command (flagged by the harness as commands_in_prose_only)
- glm-5.3--vlan-no-internet--r1: Never raised Safe Mode (Ctrl+X) before the single firewall list change over SSH
- glm-5.3--vlan-no-internet--r1: Turn 5: raised the staff-network isolation consequence of its own fix only after the fix was already live and confirmed working, rather than before proposing it
- glm-5.3--vlan-no-internet--r2: Turns 3, 4, 7: rollback commands given only in prose, never queued (flagged by the harness as commands_in_prose_only)
- glm-5.3--vlan-no-internet--r2: Never raised Safe Mode (Ctrl+X) before any of the three firewall edits made over SSH
- glm-5.3--vlan-no-internet--r2: Turn 4: first fix attempt (forward-only accept for the guest VLAN) didn't anticipate the DNS input-chain gap that the same missing-from-LAN pattern caused; only caught it after the live test failed, costing one extra round trip

