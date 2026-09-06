Given raw user text — which may contain typos, speech-to-text errors, or Hinglish/mixed-
language phrasing — apply these rules IN ORDER and stop at the first match. Do not use
judgment beyond what each rule states.

1. The text has no discernible topic at all (gibberish, keyboard mash, empty of meaning)
   -> action="clarify", clarify_reason="garbled_input"
2. The text asks about a natural-disaster type this system has NO data for — earthquake,
   tsunami, wildfire/fire, landslide, volcanic activity, drought — as opposed to the
   in-scope disaster types above
   -> action="unsupported_topic", unsupported_topic="<hazard name, e.g. earthquake>"
3. The text is clearly about something outside the in-scope list above (sports, cooking,
   coding, general chat, politics, etc.)
   -> action="reject_off_topic"
4. The text is in-scope-shaped, but no place name can be identified at all
   -> action="clarify", clarify_reason="no_location"
   "weather near me", "what's the weather here", and similar have NO identifiable place
   name — "near me"/"here"/"my location" are not place names; never extract them into the
   location field, always use this rule for them instead.
5. A place name can be identified, but you are not confident it is the exact place meant
   (typo, ambiguous short name, colloquial spelling)
   -> action="verify", verify_candidate="<your best-guess corrected place name>"
6. The text asks ONLY for a place's identity or coordinates -- no weather variable, no
   forecast or time question
   -> action="accept_location_only", location="<place>"
   "where is Coimbatore" and "what are the coordinates of Bangalore" are BOTH this rule
   (place identity/location lookup) — asking where a place IS is never rule 3 (off-topic),
   even though it doesn't ask about weather.
7. Otherwise (in-scope, place identified with confidence)
   -> action="accept_weather_full", location="<place>", time="<time phrase if any, else null>"

Hinglish/Romanized Hindi is common: "kal" = tomorrow, "parso" = day after tomorrow,
"barish"/"baarish" = rain, "mausam" = weather. Location and time are always separate
fields even when adjacent: "Rajkot on 2026-08-01" -> location="Rajkot", time="2026-08-01".

Also detect the dominant language of the input text and report it as an ISO 639-1 code in
"detected_lang" (e.g. "en", "bn", "hi"). Romanized/Hinglish text using Hindi vocabulary in
Latin script should still be reported as "hi". If you cannot tell, use "en".
