# Publish Python releases

The `publish.yml` workflow builds ABI3 wheels for CPython 3.11+: Linux
(glibc 2.17+, x86_64/aarch64), macOS (x86_64/aarch64) and Windows (x86_64).
The Intel macOS wheel is cross-compiled; native macOS tests run on ARM.

## Configure publishing

Use PyPI's [trusted publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher)
with these repository-specific values:

| Field | Value |
| --- | --- |
| Project | `hebbian-mcap` |
| Owner | `Hebbian-Robotics` |
| Repository | `mcap-tools` |
| Workflow | `publish.yml` |
| GitHub environment | `pypi` |

Create the matching GitHub environment. If its `PYPI_API_TOKEN` secret is set,
the workflow uses that token instead of trusted publishing and disables OIDC
attestations. Never commit publishing credentials.

## Build and publish

1. Run the workflow manually with publishing disabled to inspect artifacts.
2. To publish, select its `publish` input or push a `v<package-version>` tag.
   All wheel and source builds must succeed first.
3. Verify a clean consumer install:

   ```sh
   uv pip install --only-binary :all: hebbian-mcap
   ```

Consumers can then update their package versions or Git source pins.
