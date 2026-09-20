# Local compatibility patch

The vendored file is SnapDOM 3.0.0. Its image decoder normally creates a
hidden iframe and reads `contentDocument` as an optimization. AstrBot Plugin
Pages run in an iframe without `allow-same-origin`, so a nested blank iframe
has a different opaque origin and that read can raise a browser security
exception.

For this plugin, `Ca()` is changed to allocate `new Image()` in the current
document instead. This is SnapDOM's own fallback path and removes its only
`createElement("iframe")` call. Capturing actual iframe elements is not needed
for archive exports.

Patched bundle SHA-256:

```text
cb73902bed752cf4d8f9c5fea645e2c3142ae40958925dc5e541c320759d8e55
```
