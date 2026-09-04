# Spec: geoapify-provider

Module of [CAPABILITY_MAP.md](CAPABILITY_MAP.md). Status: **built and live-verified**.
Independent of the guardrail/dispatch modules — no shared dependency.

## Objective

Add Geoapify as the primary geocoder ahead of the existing keyless chain (Open-Meteo →
Nominatim → India Post), per the locked-in decision in `CAPABILITY_MAP.md`. Zero behavior
change for any deployment that doesn't set the key — the existing free chain is
untouched and still the entire resolution path when `GEOAPIFY_API_KEY` is unset.

## What was built

`app/services/location_resolver/providers/geoapify.py` — `GeoapifyGeocoder`, implementing
the existing `LocationProvider` protocol (`providers/base.py`) exactly like
`OpenMeteoGeocoder`/`NominatimGeocoder` do. `search()` returns `[]` immediately (no
exception, no log noise) when `settings.geoapify_api_key` is empty, so the resolver's
existing per-provider try/continue loop falls through to Open-Meteo/Nominatim exactly as
it did before this provider existed.

`_GEOCODERS` in `app/services/location_resolver/__init__.py` reordered to
`(GeoapifyGeocoder(), OpenMeteoGeocoder(), NominatimGeocoder())`. `GEOAPIFY_API_KEY`
added to `app/config.py` (`geoapify_api_key`) and documented in `.env.example`.

## Testing Strategy

Unit tests in `tests/test_location.py` (new section): the chain-order check, the
unconfigured-returns-empty check, `_to_candidate` parsing (full result, formatted-name
fallback, malformed input) — same depth as the existing tests for the sibling providers
(which also only unit-test `_to_candidate`, not the live HTTP call).

**Live-verified against the real API** (the user's own key, added directly to their
`.env`, never pasted into the session or printed by any command run): a coordinates-only
query for Bangalore returned `"source":"geoapify"` with correct district/state/country/
timezone fields, and an ambiguous query ("Springfield") correctly surfaced all 5 US
matches as ambiguous rather than auto-picking — proving `ranking.py`'s scoring and
dominance-margin logic works correctly against Geoapify's field shapes too, unmodified.

## Boundaries

- **Always:** `search()` must return `[]` on missing key or malformed response, never
  raise for those cases — only genuine transport failures should raise (the resolver's
  existing per-provider isolation handles that).
- **Never:** pass a country filter to the Geoapify request — same "bias not filter"
  principle `ranking.py`'s own docstring already establishes for the other providers.

## Success Criteria

- [x] `pytest -q`: 235 passed + the same 1 pre-existing unrelated failure
- [x] `ruff check app tests` / `mypy app` clean
- [x] Live: Geoapify returns correct candidates for a real query, `source` field confirms
      it fired ahead of Open-Meteo/Nominatim
- [x] Live: ambiguity detection still functions correctly against Geoapify's result shape
- [x] Zero behavior change confirmed for the unconfigured case (existing suite untouched,
      falls through to `open_meteo` exactly as before)

## Open Questions

None outstanding.
