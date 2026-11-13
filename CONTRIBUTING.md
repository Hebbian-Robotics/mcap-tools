# Contributing

Install Rust 1.92+ and uv. Run `uv sync` to build the Python extension locally.
Rust changes require rebuilding the extension with `uv run maturin develop`.

Validate with:

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test
uv run ruff check --fix
uv run ruff format
uv run ty check
uv run pytest
```

Keep Python >=3.11 support and typed public APIs. Test observable behavior using
standard MCAP readers rather than asserting Rust/Python implementation details.
Contributions are distributed under Apache-2.0.

## Python releases

The `publish.yml` workflow builds Linux (glibc 2.17+, x86_64/aarch64), macOS
(x86_64/aarch64), and Windows (x86_64) ABI3 wheels for CPython 3.11+.
The Intel macOS wheel is cross-compiled; native tests run on the ARM runner.
Set up a PyPI pending trusted publisher for project `hebbian-mcap`, owner
`Hebbian-Robotics`, repository `mcap-tools`, workflow `publish.yml`, environment
`pypi`. No API token is needed. Create a matching GitHub `pypi` environment.

Run the workflow manually without publishing to inspect artifacts. To publish,
use its `publish` input or push a `v<package-version>` tag. All wheel builds must
succeed before publishing. After release, verify a clean consumer install with
`uv pip install --only-binary :all: hebbian-mcap` before removing Git source pins.
