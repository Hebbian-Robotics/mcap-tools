# Contributing

Install Rust 1.92+ and uv, then run `uv sync`. Python 3.11 remains the minimum;
the extension uses Maturin and PyO3 ABI3 wheels. After Rust changes, rebuild the
local extension with `uv run maturin develop`.

## Validate changes

```sh
cargo install cargo-machete --version 0.9.2 --locked
cargo machete --with-metadata --skip-target-dir
cargo fmt
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test
uv run ruff check --fix
uv run ruff format
uv run ty check
uv run pytest
```

Use typed public APIs and descriptive names. Test observable results with real
MCAP readers. The Rust writer owns grouping; upstream MCAP owns serialization.
Python adapts streams and atomic path publication rather than duplicating the
file format. Keep the package independent of its consumers.

## Browser library

Use Node 24 and the pnpm version pinned in `package.json`. Run `pnpm install`,
then `pnpm format && pnpm check`. The TypeScript library ships source modules for
bundlers; `pnpm pack` checks the package contents. Keep browser utilities free
of consumer-specific topics, schemas and UI dependencies.

Browser checks run once on Linux. Changes limited to browser source or its
configuration skip the native OS/Python matrix; native changes retain existing
platform coverage.

## Update dependencies

Review Rust candidates with `cargo upgrade --dry-run --incompatible --pinned`.
Use `cargo update` and `uv lock --upgrade` to refresh resolutions, then validate
and review lockfile changes. Python resolution retains a three-day release-age
guard. Lower bounds for video extras preserve Python 3.11 compatibility; newer
compatible releases are selected for other Python versions.

Keep unwinding enabled for the PyO3 boundary. Linker and sccache settings stay
machine-local so portable wheel builds do not require a particular linker.

## CI scope and caching

Documentation-only changes run local link checks. Native changes retain the
Linux/macOS/Windows matrix on Python 3.11 and 3.14, including Python tests and
wheel smoke tests. Manual workflow runs always exercise the full matrix.
Superseded pull-request runs are cancelled; main runs finish independently so a
later documentation-only push cannot cancel outstanding code checks. Release
workflows are unaffected.

Rust dependency artifacts are cached by platform, Python version, toolchain and
compiler environment. uv also caches downloads and builds; its local-project
fingerprints still invalidate changed Rust/Python sources. Only main-branch jobs
save these caches. A cold run still builds dependencies; cache hits never skip
checks or wheel validation.

## Write documentation

Follow the [Google developer documentation style guide](https://developers.google.com/style/highlights).
Keep the README focused on installation and common tasks. Put detailed buffering,
lifecycle and export rules in [the behavior reference](docs/behavior.md). Use
sentence-case headings and descriptive links to official specifications and APIs.

Check local links with `lychee --offline --include-fragments=anchor-only README.md
CONTRIBUTING.md docs/*.md crates/mcap-tools/README.md typescript/README.md`. Remove `--offline` to
check external references. Link checking does not check spelling.

## Release and commit history

See [Python releases](docs/releases.md) for wheel builds and publishing. Squash
fixup commits before merging when useful; preserve revisions used by published
packages and Git-pinned consumers. Contributions use [Apache-2.0](LICENSE).
