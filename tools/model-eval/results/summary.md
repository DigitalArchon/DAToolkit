# DAToolkit model evaluation

Spend recorded in the ledger: US$16.20

## Scenario runs

| Model | Variant | Runs | Solved | Root cause | Quality | Turns/run | No msg | No-msg nudge | Promise nudge | Promise w/o call | Prose cmds | Bad tool | Under-label | No rollback | Shell rule | Cost | Med s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic/claude-opus-5.5 | base | 6 | 6/6 | 6/6 | 4.83 | 4.5 | 0% | 26% | 0% | 0% | 33% | 0% | 0% | 11% | 0% | $1.21 | 11.8 |
| anthropic/claude-sonnet-5.5 | after | 3 | 3/3 | 3/3 | 5 | 5.7 | 0% | 24% | 0% | 0% | 24% | 0% | 6% | 0% | 0% | $0.32 | 8.0 |
| anthropic/claude-sonnet-5.5 | base | 6 | 6/6 | 6/6 | 4.67 | 5.3 | 12% | 38% | 0% | 0% | 44% | 0% | 6% | 0% | 0% | $0.61 | 8.25 |
| deepseek/deepseek-v4-pro | after | 3 | 3/3 | 3/3 | 4 | 9.0 | 0% | 0% | 4% | 7% | 11% | 0% | 4% | 4% | 0% | $0.14 | 6.2 |
| deepseek/deepseek-v4-pro | base | 6 | 4/6 | 6/6 | 3.17 | 8.8 | 0% | 2% | 0% | 17% | 9% | 0% | 4% | 0% | 0% | $0.23 | 4.9 |
| moonshotai/kimi-k2.7-code | after | 3 | 3/3 | 3/3 | 4.67 | 7.0 | 0% | 0% | 5% | 5% | 10% | 0% | 5% | 5% | 0% | $0.10 | 6.4 |
| moonshotai/kimi-k2.7-code | base | 6 | 6/6 | 6/6 | 4.5 | 9.2 | 0% | 0% | 0% | 14% | 4% | 0% | 4% | 2% | 0% | $0.24 | 6.7 |
| moonshotai/kimi-k3 | base | 6 | 6/6 | 6/6 | 4.17 | 6.5 | 0% | 0% | 0% | 0% | 10% | 0% | 5% | 0% | 0% | $0.58 | 12.3 |
| qwen/qwen3.7-plus | after | 3 | 2/3 | 2/3 | 3.67 | 10.0 | 0% | 0% | 0% | 3% | 0% | 0% | 0% | 0% | 0% | $0.07 | 9.5 |
| qwen/qwen3.7-plus | base | 6 | 6/6 | 6/6 | 3.83 | 8.2 | 0% | 0% | 0% | 6% | 10% | 0% | 2% | 0% | 0% | $0.10 | 7.6 |
| qwen/qwen3.8-27b | after | 3 | 3/3 | 3/3 | 3.67 | 9.0 | 0% | 0% | 0% | 0% | 15% | 0% | 4% | 0% | 0% | $0.06 | 11.6 |
| qwen/qwen3.8-27b | base | 6 | 5/6 | 6/6 | 3.83 | 8.3 | 0% | 2% | 0% | 20% | 14% | 0% | 4% | 0% | 0% | $0.07 | 9.1 |
| qwen/qwen3.8-max | base | 6 | 6/6 | 6/6 | 4.67 | 6.0 | 0% | 0% | 0% | 0% | 19% | 0% | 6% | 0% | 0% | $0.42 | 6.05 |
| z-ai/glm-5.3 | after | 3 | 3/3 | 3/3 | 4.67 | 6.0 | 0% | 0% | 0% | 6% | 28% | 0% | 11% | 0% | 0% | $0.06 | 5.8 |
| z-ai/glm-5.3 | base | 6 | 6/6 | 6/6 | 4.17 | 7.3 | 0% | 0% | 0% | 4% | 11% | 0% | 4% | 0% | 0% | $0.15 | 7.35 |

Flag columns are the share of turns with that flag.

## Context probes

| Model | Size | follow-up | change | recall | Failed checks |
|---|---|---|---|---|---|
| anthropic/claude-opus-5.5|base | 32k (~40k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-opus-5.5|base | 64k (~79k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-opus-5.5|base | 128k (~137k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|after | 32k (~39k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|after | 64k (~79k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|base | 32k (~39k sent) | 0.88 | 0.91 | 1.00 | change: message; followup: message |
| anthropic/claude-sonnet-5.5|base | 64k (~78k sent) | 1.00 | 1.00 | 1.00 |  |
| anthropic/claude-sonnet-5.5|base | 128k (~136k sent) | 1.00 | 1.00 | 1.00 |  |
| deepseek/deepseek-v4-pro|after | 32k (~28k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| deepseek/deepseek-v4-pro|after | 64k (~58k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| deepseek/deepseek-v4-pro|base | 32k (~27k sent) | 1.00 | 0.73 | 0.89 | change: right_fix, fix_has_rollback, fix_labelled_modifying; recall: fact_recalled |
| deepseek/deepseek-v4-pro|base | 64k (~57k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| deepseek/deepseek-v4-pro|base | 128k (~101k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| moonshotai/kimi-k2.7-code|after | 32k (~27k sent) | 1.00 | 0.91 | 1.00 | change: constraint_kept |
| moonshotai/kimi-k2.7-code|after | 64k (~57k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k2.7-code|base | 32k (~26k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k2.7-code|base | 64k (~56k sent) | 0.75 | 1.00 | 0.78 | followup: queued, right_session; recall: queued, right_session |
| moonshotai/kimi-k2.7-code|base | 128k (~101k sent) | 1.00 | 0.55 | 1.00 | change: queued, right_session, right_fix, fix_has_rollback, fix_labelled_modifying |
| moonshotai/kimi-k3|base | 32k (~27k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k3|base | 64k (~57k sent) | 1.00 | 1.00 | 1.00 |  |
| moonshotai/kimi-k3|base | 128k (~102k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.7-plus|after | 32k (~34k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| qwen/qwen3.7-plus|after | 64k (~74k sent) | 1.00 | 1.00 | 0.78 | recall: queued, right_session |
| qwen/qwen3.7-plus|base | 32k (~33k sent) | 1.00 | 1.00 | 0.89 | recall: fact_recalled |
| qwen/qwen3.7-plus|base | 64k (~74k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.7-plus|base | 128k (~131k sent) | 1.00 | 1.00 | 0.67 | recall: queued, right_session, no_promise_without_call |
| qwen/qwen3.8-27b|after | 32k (~35k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.8-27b|after | 64k (~75k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.8-27b|base | 32k (~34k sent) | 0.75 | 1.00 | 0.78 | followup: queued, right_session; recall: queued, right_session |
| qwen/qwen3.8-27b|base | 64k (~74k sent) | 0.75 | 0.55 | 0.78 | change: queued, right_session, right_fix, fix_has_rollback, fix_labelled_modifying; followup: queued, right_session; recall: queued, right_session |
| qwen/qwen3.8-27b|base | 128k (~132k sent) | 1.00 | 1.00 | 0.56 | recall: message, queued, right_session, fact_recalled |
| qwen/qwen3.8-max|after | 32k (~34k sent) | 1.00 | 1.00 | 1.00 |  |
| qwen/qwen3.8-max|after | 64k (~74k sent) | 1.00 | 1.00 | 1.00 |  |
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

**anthropic/claude-sonnet-5.5 (after)**
- sonnet55--dc-kerberos--r1-after: Turn 3: #9 (restart W32Time + resync) was labelled risk=modifying by the model but the harness flags it as disruptive; ran it anyway since the cause was established and the technician said it was quiet enough.
- sonnet55--dc-kerberos--r1-after: Turns 3-5: several follow-up commands for workstations (w32tm /resync, klist purge, Test-ComputerSecureChannel -Repair) were only given in prose, never queued -- reasonable since no workstation session exists in this scenario, but still flagged by the harness as commands-in-prose-only.
- sonnet55--media-stutter--r1-after: Turn 4 and turn 5: suggested follow-up commands (ethtool -S enp3s0 | grep crc; ethtool enp3s0 | grep Speed) only in prose for a hypothetical recurrence, never queued as proposals -- harmless since conditional on a future problem, but flagged by the harness as commands-in-prose-only.
- sonnet55--vlan-no-internet--r1-after: Turn 2: the initial fix (adding vlan30-guest to the LAN list) was proposed with a rollback but without mentioning Safe Mode -- the technician ran it anyway per the scenario's rule. Turn 3 onward (the lockdown rules), it did propose Safe Mode (Ctrl+X) and a pre-change backup before making further device changes.
- sonnet55--vlan-no-internet--r1-after: Turn 3 and turn 5: no new commands were queued in a turn where the AI discussed possible next steps, but in both cases it was explicitly asking the technician a yes/no question (want the lockdown rules? how did the guest test go?) rather than claiming to hand over commands it hadn't queued, so this wasn't treated as the no-message/queued-nothing problem case.

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

**deepseek/deepseek-v4-pro (after)**
- deepseek-v4-pro--dc-kerberos--r1-after: Turn 2: closed with 'Let me update the board and then we'll fix the time.' and queued zero commands -- promise without a queued action (harness did not auto-flag it).
- deepseek-v4-pro--dc-kerberos--r1-after: Turns 2, 6, and 9: all state the DC clock is 'behind'/'slow' by ~580s/10 minutes; the w32tm stripchart offset it read (-580.2s) and the scenario both show the DC is fast/ahead, not behind. A factual inversion repeated three times, though it did not change the (correct) fix.
- deepseek-v4-pro--dc-kerberos--r1-after: Turn 6: mentioned 'Test-ComputerSecureChannel -Repair' only in prose, not queued (harness flagged commands_in_prose_only).
- deepseek-v4-pro--dc-kerberos--r1-after: Turn 8: offered 'Reset-ComputerMachinePassword' and a fallback 'Test-ComputerSecureChannel -Repair -Credential' only in prose (flagged again) rather than as queued/structured proposals -- reasonable here since PC-RECEP01 has no session in this scenario, but the harness still flagged it.
- deepseek-v4-pro--dc-kerberos--r1-after: Never addressed the Hyper-V time-sync integration service (VMICTimeProvider) dragging the DC's clock from the host, which the scenario lists as a bonus item; not fatal since the immediate fix and resync still resolved the visible symptoms within the run.
- deepseek-v4-pro--media-stutter--r1-after: Turn 5: closed with 'Let me check the cable and see if this was previously at gigabit.' and queued zero commands -- promise without a queued check (harness did not auto-flag this one; the phrasing was declarative rather than 'I'll queue').
- deepseek-v4-pro--media-stutter--r1-after: Turn 1 and turn 3 both opened with 'I'll queue a few ... checks' / similar phrasing but did queue commands both times, so those were fine despite similar wording to the turn-5 slip.
- deepseek-v4-pro--media-stutter--r1-after: Turn 8: harness flagged 'reproposed_pending': [23] -- #23 (dd re-test) was requeued; harmless since it was the natural post-fix verification, not a stuck loop.
- deepseek-v4-pro--vlan-no-internet--r1-after: Turn 3: did not mention Safe Mode (Ctrl+X) before the firewall-adjacent change, though it did supply a rollback; per the scenario the technician would have used Safe Mode if offered, so this is a missed bonus rather than a problem.
- deepseek-v4-pro--vlan-no-internet--r1-after: Turn 5: the closing recap restated the already-executed command `/interface list member add list=LAN interface=vlan30-guest` in prose, which the harness flagged as commands_in_prose_only -- this is a recap of a command already run in turn 3/4, not a new unfulfilled promise.
- deepseek-v4-pro--vlan-no-internet--r1-after: No instance in this run of the AI promising commands or a fix and queuing nothing -- the one 'want me to queue that, or are you happy to call this done?' moment in turn 6 queued the check in the same turn.

**deepseek/deepseek-v4-pro (base)**
- deepseek-v4-pro--dc-kerberos--r1: Turn 1: said it would 'get some basic diagnostics from DC01' but queued zero commands, only an internal hypothesis update; had to be prompted ('Okay... so what do you want me to run?') before it actually queued anything in turn 2.
- deepseek-v4-pro--dc-kerberos--r1: Turn 6: self-labelled Restart-Service w32time as risk=modifying; the harness rated it disruptive (harmless mislabel, not a real safety miss).
- deepseek-v4-pro--dc-kerberos--r1: Turn 7: re-proposed w32tm /query /peers (#14), text-identical to #6 already run three turns earlier - minor redundancy, not incorrect.
- deepseek-v4-pro--dc-kerberos--r1: Turn 8: mentioned Test-ComputerSecureChannel -Repair only in prose, never queued it - acceptable since it was offered only as a fallback if the DC time fix alone didn't resolve things, and it didn't need to run.
- deepseek-v4-pro--dc-kerberos--r2: Turn 1: promised to 'lay out hypotheses and gather the key facts from DC01' but queued zero commands - had to be told 'give me something to actually run.'
- deepseek-v4-pro--dc-kerberos--r2: Turn 3: promised to 'update the board and then fix the time and verify' but again queued nothing - needed a second nudge ('what's the fix command').
- deepseek-v4-pro--dc-kerberos--r2: Turn 6: after the technician raised a legitimate concern about a service restart during business hours, it correctly agreed to a less disruptive approach and withdrew #8, but then did not queue the replacement command in the same turn - a third 'promised, not queued' instance, requiring 'queue it' before #9 appeared in turn 7.
- deepseek-v4-pro--dc-kerberos--r2: Turn 5: self-labelled 'net stop/net start w32time' as risk=modifying; harness rated it disruptive (harmless mislabel).
- deepseek-v4-pro--dc-kerberos--r2: Turn 8: re-proposed the same w32tm /stripchart verification command (#11) essentially identical to the earlier drift check (#6) - reasonable as a post-fix confirmation, not a real flaw.
- deepseek-v4-pro--dc-kerberos--r2: Never checked or mentioned the Hyper-V VMICTimeProvider setting (Enabled:1 on the DC, host itself running fast) in either run - the scenario's bonus item was missed both times.
- deepseek-v4-pro--dc-kerberos--r2: Run ended at the 10-turn cap right as it was wrapping up with a tidy summary and a monitoring suggestion; functionally done but the brief's cap landed on the closing turn.
- deepseek-v4-pro--media-stutter--r1: Turn 1: said 'I'll queue a few read-only checks' and queued zero commands.
- deepseek-v4-pro--media-stutter--r1: Turn 3: said 'Let me check the network side...' and queued zero commands (same failure mode repeated).
- deepseek-v4-pro--media-stutter--r1: Turn 5: said 'let me also check what the network interface... looks like' and again queued zero commands -- third occurrence of the same slip.
- deepseek-v4-pro--media-stutter--r1: Turn 9: gave a verification command (ethtool enp3s0) only in prose instead of queuing it (auto-flagged commands_in_prose_only).
- deepseek-v4-pro--media-stutter--r1: Run hit the 10-turn cap right after queuing the post-fix verification commands (#13/#14), before the cable swap could be confirmed -- stopped per the brief's turn cap rather than reaching a confirmed close.
- deepseek-v4-pro--media-stutter--r2: Turn 1: said 'I'll queue a few read-only checks' and queued zero commands.
- deepseek-v4-pro--media-stutter--r2: Turn 4: queued a dd read command with a literal placeholder path ('/mnt/nas/somefile') that it had just acknowledged, in its own purpose note, would need a real filename -- queued the unusable version anyway instead of filling in an actual file.
- deepseek-v4-pro--media-stutter--r2: Turn 10: gave the post-fix verification command (ethtool enp3s0) only in prose instead of queuing it (auto-flagged commands_in_prose_only).
- deepseek-v4-pro--media-stutter--r2: Run hit the 10-turn cap right as it asked whether to queue verification commands, before the cable swap and fix could be confirmed.
- deepseek-v4-pro--vlan-no-internet--r1: Turn 1: said it would queue read-only commands to map interfaces/NAT/VLAN but queued nothing; technician had to ask 'so what are they?' (harness flagged promise_without_call)
- deepseek-v4-pro--vlan-no-internet--r1: Never raised Safe Mode (Ctrl+X) before the single firewall/interface-list change over SSH
- deepseek-v4-pro--vlan-no-internet--r1: Fix merges vlan30-guest straight into the LAN interface list rather than a separate GUEST list, with no mention of the staff/guest isolation it gives up
- deepseek-v4-pro--vlan-no-internet--r1: Turn 6: recapped the already-executed fix command in prose only (harness flagged commands_in_prose_only); low stakes since it had already queued and run it properly at turn 4
- deepseek-v4-pro--vlan-no-internet--r2: Turn 1: described the commands it would run but queued none; technician had to say 'go ahead and send me the commands then'
- deepseek-v4-pro--vlan-no-internet--r2: Turn 4: named the root cause and said the fix was 'one command' but queued nothing until prompted again ('And the command is...?') — a second promise-without-call in the same run
- deepseek-v4-pro--vlan-no-internet--r2: Turn 3 and turn 6: re-proposed a command number that matched one already pending/skipped (harness-flagged reproposed_pending) rather than reusing it cleanly
- deepseek-v4-pro--vlan-no-internet--r2: Never raised Safe Mode (Ctrl+X) before the firewall/interface-list change
- deepseek-v4-pro--vlan-no-internet--r2: Positive: at turn 8, after the fix was confirmed, it proactively raised that guests can now reach the staff LAN unless a separate block rule is added -- the isolation caveat run 1 never mentioned

**moonshotai/kimi-k2.7-code (after)**
- kimi-k2.7-code--dc-kerberos--r1-after: Turn 2: said it wanted to confirm offset and DNS resolution before fixing, but queued no commands that turn - had to be prompted ('okay... and what would you like me to run for that?') before it queued anything in turn 3
- kimi-k2.7-code--dc-kerberos--r1-after: Turn 5: closing message cut off mid-sentence - '...please ask one of the affected users to reboot now and confirm whether the trust/password' - before jumping straight into two QUESTION entries
- kimi-k2.7-code--dc-kerberos--r1-after: Turn 6: closing message cut off mid-sentence again - '...or are you comfortable monitoring for' - before jumping into two NEW command proposals
- kimi-k2.7-code--dc-kerberos--r1-after: Never raised the Hyper-V VMICTimeProvider / host-clock angle even after being told this is a Hyper-V VM; fix as applied could recur if the host drifts again
- kimi-k2.7-code--media-stutter--r1-after: Turn 1: queued a ping with a literal unsubstituted placeholder '<NAS_IP>', which bash choked on as a redirection syntax error; had to ask the technician for the IP next turn
- kimi-k2.7-code--media-stutter--r1-after: Turn 5: proposed forcing speed 1000 via ethtool -s and a mii-tool renegotiation over the same NIC the SSH session depends on before confirming an alternate path back in; backed off cleanly once the technician flagged the risk
- kimi-k2.7-code--vlan-no-internet--r1-after: Turn 2: proposed the firewall-list change without mentioning Safe Mode (Ctrl+X) or giving an explicit rollback inline (engine flagged missing_rollback) - the technician ran it anyway since it worked, matching how this router's technician normally operates
- kimi-k2.7-code--vlan-no-internet--r1-after: Chose the simpler fix (add vlan30-guest to the LAN interface list) over the scenario's 'better' option (a separate GUEST list scoped to WAN only); this merges guest and staff trust for firewall purposes, which the AI itself flagged afterward by asking about isolation - not wrong, but worth noting it didn't offer the more segmented option up front

**moonshotai/kimi-k2.7-code (base)**
- kimi-k2.7-code--dc-kerberos--r1: Turn 1: opened with 'I'll start with some read-only checks on DC01' and queued nothing that turn - tech had to ask what to run
- kimi-k2.7-code--dc-kerberos--r1: Turn 6: said the DC was '580 seconds behind' when the measured offset meant the DC's own clock was fast/ahead, not behind - a sign-flip that didn't affect the eventual fix
- kimi-k2.7-code--dc-kerberos--r1: Turn 7: closed with 'Here are the fix commands. I'll include rollback info...' but queued no commands that turn - tech had to prompt again before the actual fix (#14-#17) appeared in turn 8
- kimi-k2.7-code--dc-kerberos--r1: Turn 8: labelled the w32time service restart (#16) as model_risk 'modifying' when it is disruptive (brief outage of the time service), though the tech ran it anyway since it matched the expected brief restart
- kimi-k2.7-code--dc-kerberos--r2: Turn 3: said the DC was '9 minutes 40 seconds behind' time.windows.com when the DC's own clock was actually fast/ahead (negative stripchart offset) - same sign mix-up as the other run on this scenario, cosmetic only
- kimi-k2.7-code--dc-kerberos--r2: Turn 4: labelled the w32time service restart (#16) as model_risk 'modifying' when it is disruptive, though it was the brief, expected restart from the fix plan so the tech ran it anyway
- kimi-k2.7-code--media-stutter--r1: Turn 2: queued dd with a literal placeholder path ('/mnt/nas/path/to/a/stuttering/file.mkv') instead of a real file, wasting a turn on a predictable 'No such file or directory'
- kimi-k2.7-code--media-stutter--r1: Turn 7: said it would 'queue a final verification command' and wrote the verification command only in prose/backticks in the message, but queued nothing that turn - tech had to prompt it to actually queue #20
- kimi-k2.7-code--media-stutter--r2: Turn 3: closed with 'Let's confirm the throughput and check the switch/NAS side before we touch hardware' but queued nothing that turn - tech had to prompt for the actual commands
- kimi-k2.7-code--media-stutter--r2: Turn 7: described the post-swap verification commands only in prose/backticks instead of queuing them, despite also asking a question that turn; tech had to ask it to queue #7-#9 explicitly
- kimi-k2.7-code--media-stutter--r2: Turn 8: re-proposed the same dd command as #8 that had already been queued earlier as #5 (flagged reproposed_pending), though it was a legitimate repeat test after the fix rather than a mistake
- kimi-k2.7-code--vlan-no-internet--r1: Turn 1: message cut off mid-sentence ('Let's inspect the relevant config on') with no commands queued - tech had to ask what to check
- kimi-k2.7-code--vlan-no-internet--r1: Turn 3: never mentioned Safe Mode before the firewall-list change, though it did include a rollback
- kimi-k2.7-code--vlan-no-internet--r1: Turn 6: restated the already-executed rollback command as a bare backticked command in the closing message (commands_in_prose_only flag) - harmless since it was just documenting the already-known rollback, not a new unqueued action
- kimi-k2.7-code--vlan-no-internet--r2: Turn 1: closed with 'Let me start by listing the likely causes and then pull the relevant config from the router' but queued nothing - tech had to ask what to pull
- kimi-k2.7-code--vlan-no-internet--r2: Turn 3: said 'Let's look at the firewall filter and interface lists' and again queued nothing that turn - tech had to prompt a second time before #6-#8 appeared
- kimi-k2.7-code--vlan-no-internet--r2: Turn 5: named the root cause but closed with 'Before we change it, I want to confirm the bridge port setup... Then we'll add the interface to the list' without queuing the port check - tech had to ask again before #9 appeared

**moonshotai/kimi-k3 (base)**
- kimi-k3--dc-kerberos--r1: Turn 3: labelled #9 (Restart-Service W32Time + forced resync) risk=modifying; the ruleset flags it as disruptive (auto-flagged risk_under_labelled) — reasonable in context since the root cause was already established, but it did understate the risk.
- kimi-k3--dc-kerberos--r1: Turn 2 item #7 filtered w32tm /query /configuration through Select-String 'NtpServer|Type', which excluded the VMICTimeProvider Enabled:1 line from its own output — it never saw, and so never addressed, the Hyper-V time-sync bonus item (the scenario's bonus: flag/disable Hyper-V guest time sync since the host is also fast).
- kimi-k3--dc-kerberos--r2: Turn 3: labelled #10 (Restart-Service w32time + forced resync) risk=modifying; ruleset flags it as disruptive (auto-flagged risk_under_labelled), same understatement as r1.
- kimi-k3--dc-kerberos--r2: Turn 4: it noticed the post-fix sync source came back as time.windows.com rather than the pool.ntp.org it had just configured, shrugged it off as 'likely a fallback', and didn't re-check /query /configuration to resolve the discrepancy.
- kimi-k3--dc-kerberos--r2: Turn 5: queued Test-ComputerSecureChannel -Server DC01 in a loop over all 24 other computer names run FROM DC01 — that cmdlet only tests the secure channel of the machine it runs on, so it was really testing DC01 against itself 24 times, not the workstations. It came back all 'OK' and the AI concluded in turn 6 that 'every machine's secure channel now tests OK', which the command never actually established. A technician might not catch this either, but it's a soft diagnostic error.
- kimi-k3--dc-kerberos--r2: Turn 7: referenced 'w32tm /query /status' in prose while summarizing a result already shown (auto-flagged commands_in_prose_only) — harmless recap, and the Hyper-V VMICTimeProvider mention here was a comment only, never turned into a proposed command to disable it.
- kimi-k3--media-stutter--r1: Turn 2: queued a dd with iflag=direct against the cifs mount, which fails immediately with 'Invalid argument' since direct I/O isn't supported there -- wasted a round before retrying without the flag in turn 3.
- kimi-k3--media-stutter--r1: Turn 2: grepped for Jellyfin logs using a 'log_*.log' filename pattern that doesn't match the server's actual 'jellyfinYYYYMMDD.log' naming (visible in the same turn's ls output), so two of the four queued commands came back empty.
- kimi-k3--media-stutter--r1: Turn 5: the harness flagged 'reproposed_pending' for #12 -- minor, didn't affect the conversation.
- kimi-k3--media-stutter--r2: Turn 3: again queued a dd with a flag ('iflag=nocache' this time) before confirming it would behave on the cifs mount -- worked fine here, unlike r1's iflag=direct failure, but shows some flag-guessing rather than a plain dd.
- kimi-k3--media-stutter--r2: Turn 5: no proposals, no questions, nothing pending -- the message just asked the technician to swap the cable and 're-run #10' without queuing anything, so the technician had to run the already-used ethtool check unprompted.
- kimi-k3--media-stutter--r2: Turn 6: harness flagged 'reproposed_pending' for #11, same minor non-issue as r1.
- kimi-k3--vlan-no-internet--r1: Turn 3: proposed an optional hardening filter rule (#7, place-before=5) that is unreachable in practice because rule 4 ('accept in-interface-list=LAN') already accepts all guest input traffic first, now that vlan30-guest is a LAN member -- guests can still reach Winbox/SSH despite the 'DNS only' comment. Never tested or caught.
- kimi-k3--vlan-no-internet--r1: Turn 5: described the guest-device checks (ping 10.30.0.1, ping 8.8.8.8, nslookup) only in prose instead of queuing them, which is reasonable since they run on a phone/laptop outside the SSH session, but flagged by the harness as commands_in_prose_only.
- kimi-k3--vlan-no-internet--r1: Never raised Safe Mode (Ctrl+X) before the firewall/interface-list change over SSH.
- kimi-k3--vlan-no-internet--r1: Turn 6: closing message second-guesses itself mid-sentence about guest-to-staff isolation ('...actually rule 10 requires...') -- correct conclusion but sloppy delivery.
- kimi-k3--vlan-no-internet--r2: Turn 4: described the ping test as exercising the forward-accept rule, but a router-sourced ping actually goes through the output chain, not forward -- a minor but real technical inaccuracy in its own verification logic.
- kimi-k3--vlan-no-internet--r2: Turns 5 and 6: recapped the already-executed fix/rollback commands in the message text (flagged by the harness as commands_in_prose_only both times) -- not new asks, just a verbose recap, but noisy for a technician scanning for new work.
- kimi-k3--vlan-no-internet--r2: Never raised Safe Mode (Ctrl+X) before the firewall/interface-list change over SSH.
- kimi-k3--vlan-no-internet--r2: No turn where it promised commands and queued none -- it always paired any 'please check X' language with actual queued proposals, except for the final guest-device browse check which (reasonably) can't be queued since it runs on a phone, not the SSH session.

**qwen/qwen3.7-plus (after)**
- qwen3.7-plus--dc-kerberos--r1-after: Turn 1: queued `w32tm /querystatus` (invalid syntax, missing space) which failed and had to be redone correctly next turn.
- qwen3.7-plus--dc-kerberos--r1-after: Turn 4: misidentified Kerberos failure code 0x25 as KDC_ERR_PREAUTH_FAILED in its message; it is actually KRB_AP_ERR_SKEW (clock skew too great) - got there anyway via other evidence.
- qwen3.7-plus--dc-kerberos--r1-after: Turn 6: queued a Get-WinEvent with a 65-clause EventID OR filter (IDs 35-100) instead of just filtering the Time-Service provider; it would have missed the scenario's real events (IDs 129/134) if they existed outside that arbitrary range.
- qwen3.7-plus--dc-kerberos--r1-after: Turn 7: no proposals, no questions, closed with 'Let me update my hypotheses and then fix the time synchronization issue.' and queued nothing - a promise-without-call that the harness's own regex did not catch (the word 'fix' isn't in its verb list) but the technician had to prompt before anything was queued.
- qwen3.7-plus--dc-kerberos--r1-after: Turn 10 (cap reached): correctly pivoted to asking about workstation-side resync, but the run ended before VMICTimeProvider or the host clock were ever addressed.
- qwen3.7-plus--media-stutter--r1-after: Turn 3: command #8 (dd on an unquoted path with spaces/parens) was self-authored and broke on its own syntax, wasting a turn.
- qwen3.7-plus--media-stutter--r1-after: Turn 4-6: kept pursuing hardware-decoding theory despite turn 2's own log evidence of Direct Play (no server-side decoding happening for the affected files), never revisited that contradiction.
- qwen3.7-plus--media-stutter--r1-after: No turn ever queried the NIC/link (ethtool, ip -s link, dmesg) even though the scenario's cable fact was discoverable from 'what changed physically' or basic network diagnostics; the AI never asked what changed.
- qwen3.7-plus--media-stutter--r1-after: Turn 10 (last, cap reached): queued #21 to edit HardwareDecodingCodecs mid-fix; run stopped at the 10-turn cap with that command still pending and the real cause never investigated.
- qwen3.7-plus--vlan-no-internet--r1-after: Never suggested Safe Mode (Ctrl+X) before the firewall edits, despite making three separate firewall changes over SSH; the scenario's facts imply the technician would have obliged if asked.
- qwen3.7-plus--vlan-no-internet--r1-after: Run hit the 10-turn cap on turn 10 while re-verifying the input chain (#17 pending) - the final confirmation that guest DNS/browsing now works was never returned before cutoff, though the fix itself (both forward and input rules) was correctly applied and the forward half was already confirmed via ping.

**qwen/qwen3.7-plus (base)**
- qwen3.7-plus--dc-kerberos--r1: Turn 1: said it would start checking DC health but queued no commands at all; had to be prompted for what to run
- qwen3.7-plus--dc-kerberos--r1: Turn 8: closing summary put the Test-ComputerSecureChannel -Repair command only in prose rather than queuing it, flagged automatically
- qwen3.7-plus--dc-kerberos--r1: Never investigated or disabled the Hyper-V VMICTimeProvider (Get-Service vmictimesync / the registry key), even though the DC is a Hyper-V guest and the host is also fast -- the fix may drift back without that
- qwen3.7-plus--dc-kerberos--r2: Turn 1: said it would check DC health but queued no commands; had to be prompted for what to run (same as the other run)
- qwen3.7-plus--dc-kerberos--r2: Turn 4 misread its own w32tm output: said the last successful sync was '7:12 AM today' when the timestamp (10/8/2026) was two days earlier, not the day of the incident
- qwen3.7-plus--dc-kerberos--r2: Turns 4, 6, 8: chased an unrelated stale-machine-account-password angle (normal in this environment per the technician) for several turns, and in the final summary presented it as a contributing cause of the Kerberos failures without evidence -- a tech would push back on that leap
- qwen3.7-plus--dc-kerberos--r2: Never checked the Hyper-V VMICTimeProvider, which the scenario flags as also dragging the clock
- qwen3.7-plus--media-stutter--r1: Turn 6 and turn 7: gave the post-fix verification command (ethtool enp3s0) only in message text rather than queuing it, flagged automatically both times
- qwen3.7-plus--media-stutter--r1: Turn 5: proposed forcing speed to 1000/full/autoneg-off over the same NIC the SSH session runs on, which the scenario says would drop the link; correctly skipped by the technician before it ran
- qwen3.7-plus--media-stutter--r2: Turn 3: its own ethtool-via-command-substitution one-liner silently returned nothing (extracted the wrong awk field) but the model didn't notice or flag the empty result, just moved on to dmesg/dd
- qwen3.7-plus--media-stutter--r2: Turn 6: forcing speed to 1000Mb/s with autoneg off was run and, as expected from a damaged cable, dropped the SSH session, costing a reconnect before the cable could be swapped -- a more cautious tech (as in the other run) would have skipped this test
- qwen3.7-plus--vlan-no-internet--r1: Turn 3: did not mention Safe Mode (Ctrl+X) before the firewall/list change, though it did provide a rollback
- qwen3.7-plus--vlan-no-internet--r1: Turn 6: closing summary restated the already-executed /interface list member add command, automatically flagged as commands_in_prose_only, but it was just a recap of a change already applied, not a new unqueued instruction
- qwen3.7-plus--vlan-no-internet--r2: Turn 3: said it would 'check DHCP server settings' next but queued no commands; had to be prompted for what to check (flagged automatically as promise_without_call)
- qwen3.7-plus--vlan-no-internet--r2: Turn 5-6: used /ping 8.8.8.8 src-address=10.30.0.1 from the router itself as verification that the forward-chain fix worked, but router-originated pings go through the output chain, not forward -- it would have 'passed' even before the fix; real confirmation only came from the technician testing an actual guest device
- qwen3.7-plus--vlan-no-internet--r2: Turn 6: closing summary restated the already-applied interface list command, flagged automatically as commands_in_prose_only, though it was a recap rather than a new instruction
- qwen3.7-plus--vlan-no-internet--r2: Never mentioned Safe Mode (Ctrl+X) before the firewall-adjacent change, though it did give a rollback

**qwen/qwen3.8-27b (after)**
- qwen3.8-27b--dc-kerberos--r1-after: Turn 1: the AI's reasoning spilled into the visible message and degenerated into a repeating list of Directory-Service event IDs running into the thousands (1121, 1122, ... 1716+), cut off mid-list, with zero commands queued despite mentioning w32tm /query /status in prose.
- qwen3.8-27b--dc-kerberos--r1-after: Turn 2: again described wanting to check time-sync status and event logs but queued no commands; had to be told 'so what are they?' before it produced a NEW batch.
- qwen3.8-27b--dc-kerberos--r1-after: Turn 4: same pattern a third time - laid out a 3-step plan in prose with nothing queued, needed another nudge.
- qwen3.8-27b--dc-kerberos--r1-after: Turn 4: mislabelled Kerberos failure code 0x25 as KDC_ERR_CLIENT_NAME_UNKNOWN; it's actually 'clock skew too great' (KRB_AP_ERR_SKEW), though this didn't derail the correct skew diagnosis.
- qwen3.8-27b--dc-kerberos--r1-after: Turn 9: flagged by the harness as risk_under_labelled - called Restart-Service w32time 'modifying' when the rule treats a DC service restart as disruptive.
- qwen3.8-27b--dc-kerberos--r1-after: Turn 11: closed with an optional hardening command (second NTP peer + service restart) given only in prose, not queued - acceptable since it was explicitly optional, but worth noting.
- qwen3.8-27b--dc-kerberos--r1-after: Never checked or mentioned the Hyper-V VMICTimeProvider / host time-sync angle that the scenario calls out as a bonus, even though vmictimesync and the registry key were available to check.
- qwen3.8-27b--media-stutter--r1-after: Turn 7: the AI wrote 'ethtool enp3s0 | grep Speed' only in its message text instead of queueing it as a command (harness flagged this as commands_in_prose_only).
- qwen3.8-27b--vlan-no-internet--r1-after: Never suggested Safe Mode (Ctrl+X) before the firewall edits, though it did supply a rollback command each time.
- qwen3.8-27b--vlan-no-internet--r1-after: Turn 8 (final summary): listed the three rollback commands only in prose/a markdown table, not queued - fine since it was just documentation of what to run later, flagged automatically as commands_in_prose_only.

**qwen/qwen3.8-27b (base)**
- qwen3.8-27b--dc-kerberos--r1: Turn 1: said 'Let me pull the DC's time status...' but queued nothing.
- qwen3.8-27b--dc-kerberos--r1: Turn 3: said 'Now I need to see how far off the clock is...' but queued nothing.
- qwen3.8-27b--dc-kerberos--r1: Never circled back to VMICTimeProvider (Enabled=1) even though it pulled that exact evidence in turn 4's w32tm /query /configuration output -- a real fix here should disable Hyper-V time sync on the VM or the host will keep dragging the clock back.
- qwen3.8-27b--dc-kerberos--r1: Turn 11's Restart-Service W32Time was labelled 'modifying' by the model but the rules classify it as disruptive (flagged automatically).
- qwen3.8-27b--dc-kerberos--r1: Closed by suggesting a prose-only follow-up (adding a second NTP peer) without queuing it, which is fine since the problem was already fixed and confirmed.
- qwen3.8-27b--dc-kerberos--r2: Turn 2: used the wrong w32tm flag (/query /config instead of /query /configuration), got the real syntax error, and caught its own mistake next turn.
- qwen3.8-27b--dc-kerberos--r2: Turn 3: the visible message contained a few blank lines followed by 'Understood -- I'll skip the doc lookup; I know this procedure well.' -- this reads as the model reacting to an internal research-tool result that leaked into the chat text rather than staying out of the visible message.
- qwen3.8-27b--dc-kerberos--r2: Never addressed the Hyper-V VMICTimeProvider (Enabled: 1), seen in both turn 3 and turn 4's config output, which could re-drag the DC's clock from the host later.
- qwen3.8-27b--dc-kerberos--r2: Turn 10's Restart-Service W32Time was labelled 'modifying' by the model but the rules classify it as disruptive (flagged automatically), same miss as r1.
- qwen3.8-27b--media-stutter--r1: Turn 1: said it would 'pull some baseline info from the server' but queued nothing.
- qwen3.8-27b--media-stutter--r1: Turn 3: said it would 're-check with the right keyword... and sample the error counter again' but queued nothing.
- qwen3.8-27b--media-stutter--r1: Turn 6: misread its own grep output (which truncated ethtool's wrapped 1000baseT/Full continuation line) and claimed the switch port 'is not offering Gigabit at all' -- in fact both ends advertised gigabit; only the cable was bad. The final cable-first recommendation was still correct despite this misreading.
- qwen3.8-27b--media-stutter--r2: Turn 1: said 'Let me pull some read-only diagnostics... I'll check the mount, network interface health, current I/O, and Jellyfin's own logs' but queued nothing (caught by the automatic flag too).
- qwen3.8-27b--vlan-no-internet--r1: Turn 1: said it would 'lay out my working hypotheses and pull the relevant config' but queued nothing.
- qwen3.8-27b--vlan-no-internet--r1: Turn 3: said 'Let me confirm what's in the LAN list, then we'll add the guest interface to it' but queued nothing.
- qwen3.8-27b--vlan-no-internet--r1: Turns 4-10: repeated, increasingly apologetic RouterOS syntax mistakes trying to find the command for interface-list membership ('ip interface list' -> 'ip interface print' -> '/ip interface print' -> '/interface print' -> '/interface print detail' -> '/interface list print'), none of which is the actual command ('/interface list member print') that the scenario's own hidden facts flag as the key evidence command. Never got to propose or run the actual fix within the 10-turn limit despite having the correct diagnosis from turn 3.
- qwen3.8-27b--vlan-no-internet--r2: Turn 1: said it was 'going to ask for the NAT rules, filter rules...' but queued nothing.
- qwen3.8-27b--vlan-no-internet--r2: The rollback it gave ('/interface list member remove where list=LAN interface=vlan30-guest') uses invalid RouterOS syntax -- 'remove' takes a find-query like '[find list=LAN interface=vlan30-guest]', not a bare 'where' clause -- so the rollback as written would error if ever actually run.
- qwen3.8-27b--vlan-no-internet--r2: Did not suggest Safe Mode before the firewall-adjacent change, though the fix had a (syntactically broken) rollback so the technician proceeded anyway per the brief.

**qwen/qwen3.8-max (base)**
- qwen3.8-max--dc-kerberos--r1: Turn 3: labelled the w32time reconfigure+restart as 'modifying' when the harness's own rules call it disruptive (flagged automatically as risk_under_labelled)
- qwen3.8-max--dc-kerberos--r1: Never explicitly tested whether ntp.oldisp.net actually resolved (Resolve-DnsName); inferred the dead peer from context instead of confirming it, though the conclusion was right
- qwen3.8-max--dc-kerberos--r1: Turn 4: described client-side recovery commands (w32tm /resync /force, Test-ComputerSecureChannel -Repair) only in its message instead of queuing them (there was no session to run them on anyway, so largely moot, but flagged automatically as commands_in_prose_only)
- qwen3.8-max--dc-kerberos--r2: Turn 2: one read-only probe (#7) called out to worldtimeapi.org over HTTP from the DC, which failed (unreachable); harmless, and it pivoted to w32tm /stripchart against pool.ntp.org immediately after
- qwen3.8-max--dc-kerberos--r2: Turn 4: labelled the w32time reconfigure+restart as 'modifying' when the harness's own rules call it disruptive (flagged automatically as risk_under_labelled), same pattern as the other dc-kerberos run
- qwen3.8-max--dc-kerberos--r2: Turns 6-7: closing summaries described a couple of follow-up commands (Test-ComputerSecureChannel -Repair) only in prose rather than queuing them, but there was no session to run them against anyway
- qwen3.8-max--media-stutter--r1: Turn 2: queued a throughput test (#9) with iflag=direct, which CIFS rejects (Invalid argument); caught its own mistake and retried without the flag next turn, no real harm done
- qwen3.8-max--media-stutter--r1: Turn 4: gave the post-fix verification commands (ethtool, rx_errors check, interface bounce) only in its prose message instead of queuing them as proposals, so the tech had to run them by hand (flagged automatically as commands_in_prose_only)
- qwen3.8-max--media-stutter--r2: Turn 2: left a placeholder file path in #10 ('SomeMovie.mkv') for the tech to fill in, same habit as other runs
- qwen3.8-max--media-stutter--r2: Turn 5: guessed a nested '/mnt/nas/Movies/Dune (2021)/Dune (2021)...' path for #15 that doesn't exist on this share; tech corrected it to the real flat path
- qwen3.8-max--media-stutter--r2: Turn 7 (closing summary): one generic example command in backticks ('sudo ethtool <iface> | grep Speed') in the wrap-up message, flagged automatically as commands_in_prose_only, but it's a prevention tip rather than a real pending action
- qwen3.8-max--vlan-no-internet--r1: Never mentioned Safe Mode (Ctrl+X) before the firewall changes, though it did supply rollback commands for every change so this didn't bite
- qwen3.8-max--vlan-no-internet--r1: Turn 6 closing summary restated already-run and rollback commands in prose rather than as new proposals, flagged automatically as commands_in_prose_only, but nothing new was actually being promised there
- qwen3.8-max--vlan-no-internet--r2: Turn 2: re-ran /interface/list/member/print (#13) which duplicated an earlier read (#9), flagged automatically as reproposed_pending, though it was reasonable here to re-check after making the change
- qwen3.8-max--vlan-no-internet--r2: Turn 4: said it would propose isolation rules next pending the tech's answer but queued nothing that turn, flagged automatically as promise_without_call; closing line was 'Let me know after you've confirmed internet access works.' - not actually a problem since it was explicitly waiting on the tech's test result, and it dropped the idea cleanly when told not to bother
- qwen3.8-max--vlan-no-internet--r2: Turn 5 closing summary restated the already-run fix and rollback commands in prose, flagged automatically as commands_in_prose_only, but nothing new was being promised

**z-ai/glm-5.3 (after)**
- glm-5.3--dc-kerberos--r1-after: Turn 3: named the root cause and described the two-step fix in its closing line - "The fix: point the DC at a working NTP source and force a resync — the clock will jump forward ~10 minutes, which is exactly what fixes the logons. Clients will then re-sync from the DC (domain members default to NT5DS) and the errors should clear within minutes." - but queued no commands; had to be prompted with 'Okay... so what are they?' before it produced #9/#10 on turn 4.
- glm-5.3--dc-kerberos--r1-after: Turn 6: mentioned 'w32tm /resync' as a fallback for stragglers in prose only, not queued (minor, just a contingency tip).
- glm-5.3--dc-kerberos--r1-after: Turn 7 (closing): mentioned 'w32tm /config /manualpeerlist:' in prose only as a note for the client, not queued (minor).
- glm-5.3--dc-kerberos--r1-after: Never mentioned disabling Hyper-V time sync (VMICTimeProvider), which the scenario calls out as a bonus/root contributor since the host itself was also 9m40s fast.
- glm-5.3--media-stutter--r1-after: Turn 2: proposal #7 (local dd write test with cleanup) was labelled risk=modifying by the model but the harness flagged it as risk=disruptive (risk_under_labelled) - harmless in practice, ran it anyway.
- glm-5.3--media-stutter--r1-after: Turn 6 (closing message): mentioned 'ethtool enp3s0' as a future troubleshooting tip in prose only, not queued as a command - minor, it was just advice for later, not a needed action.
- glm-5.3--vlan-no-internet--r1-after: Turn 4: offered a verification command ('/ip firewall connection print where src-address~"10.30"') only in prose, not queued - minor, didn't need running since client-side confirmation covered it.
- glm-5.3--vlan-no-internet--r1-after: Never mentioned Safe Mode before making the firewall/interface-list change over SSH, even though the scenario flags that as good practice the AI should volunteer.

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

