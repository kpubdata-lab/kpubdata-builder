# Builder license inventory plan (#847)

## Scope

- Record every locked Python distribution with version, declared license, source,
  metadata basis, and install scope. Keep unknown or non-obvious declarations for
  maintainer review.
- Inventory tracked third-party or generated data separately from this repository's
  MIT code license. Identify which entries enter the wheel, source archive, or image.
- Ship the generated notice with the wheel and container; keep the inventory and
  notice synchronized with a CI gate.
- Record the pinned container inputs and the OS package inventory gap; the image
  cannot be inspected without a container runtime.

## Files

`scripts/license_inventory.py`, its tests, `docs/compliance/license_inventory.json`,
`THIRD_PARTY_NOTICES.md`, `pyproject.toml`, `Dockerfile`, `.dockerignore`,
`.github/workflows/ci.yml`, and `CHANGELOG.md`.

## Risks and verification

- Package metadata is a declaration, not a legal finding. Missing or version-mismatched
  metadata remains marked for review.
- The container base is mutable by tag and `apt-get upgrade`; a built-image SBOM is
  needed for exact OS package versions and license texts.
- Test gate failures for lock drift, missing asset coverage, stale notice, and missing
  inventory records; run lint/type checks, build both distributions, inspect archive
  contents, and check CI before merging.
