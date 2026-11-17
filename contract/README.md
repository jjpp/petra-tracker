# API contract

`petra-contract.v1.json` describes the shape of the (undocumented) Petra GlobalApi responses
that petra-tracker relies on: required/optional fields with types, known enum values, and the
coordinate-encoding rules.

- The Home Assistant integration ships a copy as
  `custom_components/petra_tracker/core/contract.json`; a test asserts the two are identical.
- The Android app will validate against the same file.

When the API changes (reported by `petra_tracker_schema_drift`), update the canonical file here,
bump `schema_version` if the change is incompatible, copy it into the integration and adapt the
parsers. Use synthetic examples only when documenting changes.
