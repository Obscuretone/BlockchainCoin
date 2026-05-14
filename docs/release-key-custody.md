# Release Key Custody

BlockchainCoin release signatures are Ed25519 detached manifest signatures. A release is acceptable only when the artifact digests match the manifest and the manifest signature verifies against an approved public-key fingerprint.

## Roles

- Release owner: prepares artifacts, manifest, and signature.
- Release reviewer: verifies source revision, quality output, artifact digests, and public-key fingerprint before publication.
- Key custodian: owns encrypted signing-key backups and rotation records.

No one should both approve a release and be the only person with access to the signing key.

## Storage

- Store the release signing wallet encrypted with a unique passphrase.
- Keep the primary key offline except during signing.
- Keep at least one encrypted offline backup with the key custodian.
- Never commit release signing wallets, passphrases, or recovery material.
- Publish only the public key and its fingerprint.

## Signing

1. Build from a clean checkout using `constraints/release.txt`.
2. Generate the deterministic SHA-256 release manifest.
3. Sign the manifest with the encrypted release wallet.
4. Publish artifacts, manifest, manifest signature, public key, and public-key fingerprint together.
5. Have the release reviewer verify the signature by fingerprint before announcing the release.

## Rotation

Rotate the release signing key when a custodian changes, a key or passphrase may have been exposed, an offline backup is lost, or the regular annual rotation window arrives.

Rotation steps:

1. Freeze releases until the new key is created and reviewed.
2. Generate a new encrypted release wallet offline.
3. Record the old and new public-key fingerprints in the release notes.
4. Sign the next release manifest with the new key.
5. Revoke trust in the old fingerprint after the overlap release is verified.
6. Update `SECURITY.md`, `docs/reproducible-builds.md`, and the release notes with the active fingerprint.

## Compromise

If a release key is suspected compromised, follow `docs/incident-response.md`, pause releases, publish the affected fingerprint, rotate immediately, and require reviewers to reject signatures from the compromised fingerprint.
