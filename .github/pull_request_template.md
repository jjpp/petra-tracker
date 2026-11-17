## What and why

<!-- Short description of the change. -->

## Checklist

- [ ] Tests added/updated, `pytest` and `ruff check .` pass
- [ ] **No real data**: no names, phone numbers, addresses, ID numbers, booking/car/driver/user IDs,
      real coordinates or GPS tracks, tokens or API keys — fixtures are synthetic
      (`tests/synthetic.py`); `python tools/check_public_hygiene.py` passes
- [ ] If the API contract changed: `contract/petra-contract.v1.json` updated (and copied into
      `custom_components/petra_tracker/core/contract.json`)
