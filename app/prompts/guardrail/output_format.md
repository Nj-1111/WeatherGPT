CAVEAT FOR ANYONE EDITING THIS FILE: the JSON keys and enum values below are read by
app/services/query_guardrail.py's _parse() and the GuardrailAction/ClarifyReason enums in
app/schemas/query.py. Wording around them is free to change; renaming a key or an enum
value here requires the matching code change too, or parsing silently falls back to the
deterministic path. Takes effect on next process restart, not instantly.

Output ONLY a single valid JSON object, no markdown fences, no commentary:
{"action": "accept_location_only"|"accept_weather_full"|"reject_off_topic"|"clarify"|
"verify"|"unsupported_topic", "location": string|null, "time": string|null,
"verify_candidate": string|null, "clarify_reason": "garbled_input"|"no_location"|null,
"unsupported_topic": string|null, "confidence": number, "detected_lang": string}
