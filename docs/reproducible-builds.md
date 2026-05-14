# Reproducible Builds

BlockchainCoin release artifacts should be built from a clean checkout with an explicit Python version and the pinned release constraints in `constraints/release.txt`. CI also exports `PIP_CONSTRAINT` so isolated package build dependencies are constrained.

Recommended local release check:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -c constraints/release.txt -e '.[dev]'
ruff format .
ruff check .
pyright
coverage run -m unittest
coverage report
python -m compileall -q blockchaincoin tests
rm -rf dist
PIP_CONSTRAINT=constraints/release.txt python -m build
```

After building, generate a manifest and detached signature from the exact artifacts that will be published:

```python
from pathlib import Path
from blockchaincoin import Wallet
from blockchaincoin.release import (
    build_release_manifest,
    release_public_key_fingerprint,
    sign_release_manifest,
)

artifacts = sorted(Path("dist").glob("*"))
manifest = build_release_manifest(artifacts)
Path("dist/blockchaincoin-0.1.0-manifest.json").write_text(
    manifest.to_json(),
    encoding="utf-8",
)
release_key = Wallet.load("release-signing-key.json", passphrase="release passphrase")
signature = sign_release_manifest(manifest, release_key)
Path("dist/blockchaincoin-0.1.0-manifest.sig.json").write_text(
    signature.to_json(),
    encoding="utf-8",
)
print(release_public_key_fingerprint(release_key.public_key))
```

The manifest records each artifact filename, byte size, and SHA-256 digest in a deterministic JSON shape. Reviewers should rebuild from the same source revision and compare the artifact digests before trusting a release. The detached manifest signature must verify with the published release public-key fingerprint before any artifact is accepted. Custody and rotation requirements are in `docs/release-key-custody.md`.
