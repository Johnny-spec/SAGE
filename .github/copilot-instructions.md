# Project Instructions

## Fast Change Workflow

Apply these rules to every code modification request in this repository:

1. Start from the concrete file, symbol, failing behavior, command, or test named by the user. Read only enough nearby code to identify the controlling path.
2. Form one local hypothesis and one cheap check that could disprove it, then make the smallest grounded edit. Do not map unrelated parts of the repository first.
3. Keep the change strictly within the requested behavior. Do not perform unrelated refactors, formatting, documentation updates, or cleanup.
4. Immediately after the first substantive edit, run the narrowest relevant test, type check, lint check, syntax check, or behavior check. Repair and rerun that same focused check when it exposes a local defect.
5. Do not run the full test suite by default. Run broader validation only when the user requests it, the change affects a shared or cross-module contract, or focused checks cannot establish reasonable confidence.
6. Ask a question only when unresolved ambiguity blocks a correct implementation. Otherwise, state a reasonable local assumption and proceed.
7. Keep progress updates brief and action-oriented. Do not delay implementation to produce a lengthy plan or repository survey.
8. Stop once the requested behavior is implemented and focused validation passes. Report the changed files, validation performed, and any concrete residual risk.

Increase investigation and validation proportionally for authentication, authorization, secrets, destructive data operations, concurrency, migrations, public APIs, deployment, or other high-impact changes. Speed must not bypass correctness or safety checks appropriate to the actual risk.

## Code Organization and Test Scope

Apply these constraints to every code modification request:

1. Keep each new or modified source file at no more than 600 lines. Split a page, component, or module by responsibility before it exceeds this limit. When an existing file already exceeds 600 lines, do not grow it; extract only the functionality touched by the request when that can be done without unrelated refactoring.
2. Place code for the same feature in the same feature folder. Keep its page, components, services, models, and focused tests together when the existing architecture supports that layout; do not scatter one feature across unrelated folders.
3. Keep test changes minimal. Add or update only the basic happy-path test and the smallest critical boundary or regression test needed for the requested behavior. Do not generate exhaustive, repetitive, or low-value test files unless the user explicitly requests broader coverage or the change is high risk.
4. Preserve the project's established architecture and directory conventions. Put new files beside the analogous existing feature or layer, and extend the current structure instead of introducing a parallel organization model. If the requested placement conflicts with the architecture, follow the architecture and briefly explain the placement.