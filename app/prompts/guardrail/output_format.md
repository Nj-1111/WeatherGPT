CAVEAT FOR ANYONE EDITING THIS FILE: the JSON keys and enum values below are read by
app/services/input_pipeline/query_guardrail.py's _parse() and the GuardrailAction/ClarifyReason enums in
app/schemas/query.py. Wording around them is free to change; renaming a key or an enum
value here requires the matching code change too, or parsing silently falls back to the
deterministic path. Takes effect on next process restart, not instantly.

Output ONLY a single valid JSON object, no markdown fences, no commentary:
{"action": "accept_location_only"|"accept_weather_full"|"reject_off_topic"|"clarify"|
"verify"|"unsupported_topic"|"greeting", "locations": [string, ...], "time_phrases": [string, ...],
"pairing_mode": "locations_x_shared_time"|"times_x_shared_location"|"full_cross_product",
"verify_candidate": string|null, "clarify_reason": "garbled_input"|"no_location"|null,
"unsupported_topic": string|null, "confidence": number, "detected_lang": string,
"apparent_context": string|null, "capabilities": [string, ...],
"confidence_per_capability": {string: "low"|"medium"|"high", ...},
"reasoning": string}

"reasoning" is a short (under 25 words) internal note on which rule fired and why —
logged for debugging, never shown to the user. State explicitly when rule 0 (conversation
continuation) applied.

"capabilities" must only ever contain values from this exact list: temperature,
precipitation, wind, marine, extreme_events, humidity, pressure, cloud_cover, visibility,
heat_stress, travel_safety_guidance.
