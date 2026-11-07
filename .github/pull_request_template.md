## What changes

## Why

## How it was tested

- [ ] `make check` passes (needs `promtool` and `kubectl` on the PATH)
- [ ] `ifp generate --check` passes; regenerated rules and dashboards are committed
- [ ] Gateway, server or controller changes: `ifp drill` still promotes the healthy canary and rolls back the others
- [ ] `CHANGELOG.md` updated
