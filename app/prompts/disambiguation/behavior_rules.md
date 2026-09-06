You will be given a numbered list of candidate places (name, state/region, country) and
the user's free-text reply to a question that asked them to pick one. Apply these rules in
order:

1. If the reply clearly names or strongly implies one specific candidate — by state,
   country, an ordinal ("the first one", "the second"), or by repeating a distinguishing
   detail from the list — return that candidate's index.
2. If the reply could plausibly match more than one candidate, or names none of them,
   return null. Do not guess between close options.
3. If the reply does not attempt to answer the question at all (it reads as a new,
   unrelated question, or a correction to something else entirely), return null so the
   system treats it as a fresh query rather than forcing a match.
4. Never propose, infer, or return a place that is not one of the given indices. If you
   are not confident, null is always the correct answer over a guess.
