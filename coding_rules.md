# Coding Rules

## General Principles
1. Write clean, minimal, production-grade code. No exceptions.
2. Simplicity over cleverness. Fewer moving parts, fewer files, fewer abstractions.
3. Every line must justify its existence. If it is not used, delete it.
4. Optimize for readability first, performance second, brevity third — but never sacrifice performance for style.

## Comments
5. No unnecessary comments. Code should be self-explanatory through naming and structure.
6. Only comment non-obvious logic (e.g. algorithmic tricks, external constraints, workarounds).
7. No commented-out code left in files. Delete dead code instead of disabling it.
8. No TODO comments left unresolved. Fix it now or track it in an issue tracker, not inline.

## Debugging / Output
9. No print statements or debug logging left in final code.
10. Use a proper logging framework only when logging is a real requirement, with appropriate log levels (error, warning, info). No debug-level logs in production paths.
11. No emojis anywhere in code, comments, commit messages, or logs.

## Imports and Dependencies
12. Remove all unused imports before finalizing a file.
13. Remove all unused packages/dependencies from requirements files, package.json, etc.
14. Do not import an entire module/package when only one function/class is needed, if selective import is supported.
15. No duplicate or redundant dependencies achieving the same purpose.
16. Pin dependency versions; do not leave loose/unpinned versions in production.

## Structure and Organization
17. One clear responsibility per function. One clear responsibility per file/module.
18. Keep functions short; extract logic when a function exceeds a reasonable single-purpose length.
19. Group related code logically (models, services, utils, config) — no dumping everything into one file.
20. Consistent naming convention across the entire project (no mixing camelCase and snake_case in the same language context).
21. No dead files, no unused functions, no unused variables, no unused classes.

## Performance and Efficiency
22. Avoid unnecessary loops, nested loops, or repeated computation — cache/memoize where it measurably helps.
23. Avoid unnecessary object/data copies; prefer in-place or reference operations when safe.
24. Avoid premature I/O, network, or DB calls inside loops — batch where possible.
25. Use efficient data structures appropriate to the access pattern (set for membership checks, dict for lookups, etc).
26. Minimize third-party dependency overhead — do not add a library for something solvable in a few lines.
27. Lazy-load or defer expensive operations until actually needed.

## Error Handling
28. Handle errors explicitly; no silent except/catch blocks that swallow exceptions.
29. Fail fast and clearly — raise meaningful errors, not generic ones.

## Environment / Housekeeping
30. Delete `__pycache__`, `.pyc`, and other build/cache artifacts after finishing work on a file.
31. Remove unused virtual environments, unused installed packages, and stale lock file entries after finishing a task.
32. Keep `.gitignore` updated to prevent cache/build artifacts from being tracked.
33. No leftover temporary files, test scripts, or scratch files in the final project directory.

## Final Check Before Completion
34. Re-scan the file: remove unused imports, unused variables, dead code, debug prints, and stray comments.
35. Confirm the code runs with minimal overhead and no unnecessary dependencies before marking the task done.
