# Agent notes

## Visible version

The generated page shows a semantic version, `MAJOR.MINOR.PATCH`, from `SITE_VERSION` in `src/op_usage/__init__.py`. That constant is the only place the version is stored. The page prints it as fine print at the bottom (`Version 1.0.0`).

Every change, however small, bumps that visible version:

- **PATCH** for tiny fixes, tweaks, and docs.
- **MINOR** for features or noticeable behavior changes, resetting PATCH to 0.
- **MAJOR** for big overhauls or breaking data changes, resetting MINOR and PATCH to 0.

Leave the package version, parser version, and schema version alone unless that change is specifically about them.
