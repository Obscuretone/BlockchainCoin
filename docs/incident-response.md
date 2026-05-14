# Incident Response

BlockchainCoin is not ready to secure public funds. This runbook exists so security reports, consensus bugs, release mistakes, and peer-network incidents are handled consistently while the project matures.

## Severity

- Critical: private-key exposure, consensus inflation, arbitrary code execution, release-artifact compromise, or a remote crash affecting default node operation.
- High: consensus divergence, durable-state corruption, reproducible denial of service, or signature/serialization bypass without confirmed public exploitation.
- Medium: local data loss, wallet handling weakness, mempool/peer policy bypass, or CLI behavior that can mislead operators.
- Low: hardening gaps, documentation inaccuracies, or defense-in-depth findings.

## Intake

1. Acknowledge receipt privately.
2. Record reporter contact, affected version or commit, reproduction steps, and suspected impact.
3. Create a private tracking issue or advisory draft.
4. Assign one incident owner and one reviewer.
5. Preserve original reports, logs, crash traces, payloads, and artifact hashes.

## Triage

1. Reproduce in an isolated checkout.
2. Identify affected components: wallet, consensus, storage, mempool, peer protocol, CLI, packaging, or documentation.
3. Decide whether the issue can affect funds, consensus safety, node liveness, release integrity, or operator privacy.
4. Set severity and target response time.
5. If exploitation is plausible, pause releases until containment is complete.

## Containment

1. Stop publishing new artifacts from affected branches.
2. Revoke or rotate exposed credentials, signing keys, tokens, or automation secrets.
3. Disable unsafe network entry points or recommend operators shut down affected services.
4. Mark malicious or invalid inputs in persistent caches when applicable.
5. Avoid public details until a fix or mitigation is ready.

## Remediation

1. Add a failing regression test or fuzz/property reproducer.
2. Patch the smallest consensus-safe surface.
3. Run Ruff, Pyright, coverage, compileall, and package build.
4. Rebuild artifacts and generate a deterministic manifest.
5. Have an independent reviewer inspect the patch and release notes.

## Communication

1. Keep the reporter updated at meaningful milestones.
2. Publish a security advisory when users need action.
3. Include affected versions, fixed version, severity, impact, mitigations, and artifact hashes.
4. Credit reporters unless they request anonymity.
5. Do not disclose exploit payloads before a reasonable upgrade window.

## Recovery

1. Publish fixed artifacts and manifests.
2. Update documentation and operational guidance.
3. Verify clean installs and upgrades from the previous release.
4. Monitor for repeated invalid blocks, peer bans, crashes, or support reports.
5. Close the incident only after mitigations are verified.

## Postmortem

1. Document timeline, root cause, detection gap, blast radius, and tests added.
2. List follow-up hardening tasks with owners.
3. Update this runbook if the response exposed missing steps.
4. Keep private sensitive details out of public postmortems.
