# 03 — AVANT entity matcher keeps digit-only tokens, unlike its four siblings

Status: needs-triage
Spec: `../spec.md`
Type: task
Blocked by: —

**Unstarted. Out of scope for the Tier A work.** Recorded so it is not rediscovered.

## Finding

`domain_adapters/avant.py:647 _entity_tokens` is the only one of the five `entity_key`-style
matchers with no `.isdigit()` filter:

| Matcher | min length | drops digit-only tokens |
| --- | --- | --- |
| `domain_manager_fallback.py:748 entity_key` | 2 | yes |
| `domain_adapters/amista.py:267 _entity_key` | 2 | yes |
| `domain_adapters/bhs.py:251 _entity_key` | 2 | yes |
| `domain_adapters/porovnejfondy.py:317 _entity_key` | 2 | yes (lines 330, 333 — spelled as separate statements) |
| `domain_adapters/avant.py:647 _entity_tokens` | 2 | **no** |

A fund distinguished only by a numeral compares differently under avant's matcher than under the
other four. No test covers a digit-bearing fund name through the avant adapter — `tests/test_avant_adapter.py`
has no such case.

Note for whoever picks this up: an earlier review claimed `porovnejfondy` was the outlier. That was
wrong; it has both filters. `avant` is the one.

## Why it was deferred

It sits in Tier B, which the Tier A work deliberately does not touch. Changing it moves adapter
matching for a live fund set and would need its own offline before/after diff — it cannot ride on a
bug fix.

## If picked up

Establish first whether any fund in `data/input/funds.json` is actually distinguished by a
digit-only token. The spec records that `Jet 2/3/4 SICAV`, `JTFG FUND I/V` and `ČCE (A)/(B)` already
collide for a different reason — their differentiating character is dropped by the minimum-length
filter in *every* matcher, avant included. If those are the only digit-bearing cases, aligning
avant changes nothing observable and the fix is cosmetic consistency, not a defect repair. Decide
on that evidence before editing.

## Comments
