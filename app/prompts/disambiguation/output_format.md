CAVEAT FOR ANYONE EDITING THIS FILE: "selected_index" is read directly by the
disambiguation parser to index into the candidate list held in session state. Renaming
this key requires the matching code change. Takes effect on next process restart.

Output ONLY a single valid JSON object, no markdown fences, no commentary:
{"selected_index": integer|null}
