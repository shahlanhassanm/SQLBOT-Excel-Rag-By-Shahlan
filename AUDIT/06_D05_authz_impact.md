# AUDIT / 06 — D-05 Authorization Impact Report

**Status: awaiting approval. No code has been changed for D-05.**

Requested before any behavioural change to authorization. This report traces
every call site, states current vs proposed behaviour, and lays out options
without choosing one.

---

## 1. The predicate

```python
# backend/apps/datasource/crud/permission.py:96
def is_normal_user(current_user: CurrentUser):
    return current_user.id != 1
```

One boolean, derived solely from the primary key. It is used for **two logically
different questions** at six call sites.

## 2. Three coexisting notions of "privileged"

| # | Definition | Where | Used for |
|---|---|---|---|
| A | `current_user.id != 1` | `permission.py:96` | row + column permission bypass, **and** the fanout gate |
| B | `userInfo.id == 1 and userInfo.account == 'admin'` | `crud/user.py:32` (sets `isAdmin`), duplicated at `mcp.py:67` | route-level `@require_permissions`, workspace admin screens |
| C | `current_user.weight == 0` (workspace role rank) | `schemas/permission.py:81`, `api/workspace.py:65,95,130` | `ws_admin` role checks |

**A is strictly weaker than B.** A user whose `id == 1` but whose account is not
`'admin'` bypasses all row/column permissions (A) while *not* being an admin for
route authorization (B). Nothing enforces that user 1 is the seeded admin.

## 3. Every call site

| # | Site | Question being asked | Effect when `is_normal_user()` is **True** (id ≠ 1) |
|---|---|---|---|
| 1 | `permission.py:24` `get_row_permission_filters` | "should row rules apply?" | Row filters **are** computed and returned |
| 2 | `permission.py:51` `get_column_permission_fields` | "should column rules apply?" | Column filters **are** applied |
| 3 | `crud/datasource.py:329` `preview()` | "should preview be filtered?" | Preview **is** row+column filtered |
| 4 | `crud/datasource.py:658` `get_tables_sample_data` *(added by D-02)* | "must we fail closed if the rule lookup fails?" | Fails closed — no samples |
| 5 | `value_index.py:295` `build_value_hints` *(added by D-02)* | same | Fails closed — no hints |
| 6 | `llm.py:2337` SQL permission rewrite | "should the generated SQL be rewritten with row filters?" | Rewrite **runs** |
| 7 | **`llm.py:1061` `decompose_question`** | **"is it safe to run unfiltered secondary legs?"** | **Returns `single` — fanout/split is DISABLED** |

Sites 1–6 all ask *"does this user need permission enforcement?"* — and answer
correctly for every user except id 1.

**Site 7 asks the opposite question** and reuses the same predicate, so it reads
as *"only user 1 may use fanout"*. Its own comment states the intent differently:

```python
# Conservative gates: auto-selected ds only, no assistant context, and not a
# row-permission restricted user (secondary legs bypass the permission rewrite).
```

"Not a row-permission **restricted** user" ≠ "is user 1". A user with **no row
rules configured** is not restricted, yet is still blocked today.

## 4. Current vs proposed behaviour

### 4.1 Current

| Actor | Row/col permissions | Fanout / cross-file |
|---|---|---|
| User id 1 (seeded `admin`) | **Bypassed entirely** | **Enabled** |
| User id 1, account renamed | **Bypassed entirely** | Enabled |
| Any other user — workspace admin | Enforced | **Disabled** |
| Any other user — no row rules at all | Enforced (no-op) | **Disabled** |
| Any other user — restricted | Enforced | Disabled |

Consequence: `AGENTIC_DECOMPOSE_ENABLED: "true"` in `docker-compose.yaml:64` and
the cross-file capability described in `important.md` are, in practice,
**single-account features**.

### 4.2 Proposed — the two questions separated

| Actor | Row/col permissions | Fanout / cross-file |
|---|---|---|
| Platform admin (definition B) | Bypassed | Enabled |
| Any user with **no** applicable row rules | Enforced (no-op) | **Enabled** ← changed |
| Any user **with** row rules | Enforced | Disabled (unchanged) |

Site 7 would become a check on *whether row filters actually exist for this
user and datasource*, which is already computable:

```python
if collect_row_filters(session, self.current_user, self.ds, tables):
    return single
```

## 5. Files affected

| File | Sites | Change class |
|---|---|---|
| `apps/datasource/crud/permission.py` | 1, 2 (+ the predicate) | Authorization semantics |
| `apps/datasource/crud/datasource.py` | 3, 4 | Follows the predicate |
| `apps/datasource/value_index.py` | 5 | Follows the predicate |
| `apps/chat/task/llm.py` | 6, 7 | 6 follows; **7 is a different question** |
| `apps/system/crud/user.py`, `apps/mcp/mcp.py` | — | Where definition B lives (duplicated) |

## 6. Security implications

| Change | Direction | Notes |
|---|---|---|
| Replace A with B (`isAdmin`) at sites 1–6 | **Tightens** | A user with `id == 1` but a non-`admin` account stops bypassing permissions. Verified: `isAdmin` is `id == 1 AND account == 'admin'`, so B ⊂ A — no user gains a bypass. |
| Replace A with C (`weight == 0`) | **Loosens — do not do** | Every workspace admin would bypass row rules. |
| Change site 7 to "has row filters" | **Neutral-to-tightening** | Fanout legs bypass the permission rewrite (`_run_secondary_leg` never calls `generate_filter`), so gating on *actual* filters is a strictly more accurate expression of the existing safety property. A user who acquires a row rule mid-session correctly loses fanout. |
| Leave as-is | Status quo | The bypass persists; fanout stays single-account. |

**Residual risk if site 7 is changed:** it must be evaluated against the
**same** table set the legs will query. Legs switch datasource via
`_apply_datasource`, so the check has to run per leg, not once up front — or the
gate must be conservative (any row rule anywhere in the workspace ⇒ no fanout).
The conservative form is recommended for a first change.

## 7. Functional implications

- **Sites 1–6 with B:** no functional change for any user except a
  non-`admin`-accounted id 1, who would begin having permissions enforced. If
  such an account exists in production it may see fewer rows than before —
  correct, but visible.
- **Site 7:** cross-file fanout becomes available to ordinary users for the
  first time. This is a **latency and cost** change as well as a functional one:
  each fanout leg is an extra LLM call plus an extra query
  (`AGENTIC_FANOUT_MAX_SOURCES` = 5). It also means the feature starts being
  exercised in production, where it has never run.
- `important.md`'s claims about cross-file answers become true.

## 8. Options

### Option 1 — Split the predicate, fix site 7 only *(smallest, recommended first step)*
Leave `is_normal_user` untouched. Change only `llm.py:1061` to gate on actual
row filters.
*Security:* neutral-to-tightening. *Functional:* fanout enabled for unrestricted
users. *Risk:* low. *Blast radius:* 1 file, 1 condition.
**Does not fix the id-1 bypass.**

### Option 2 — Retarget sites 1–6 to `isAdmin` *(fixes the bypass)*
Replace the body with `return not getattr(current_user, 'isAdmin', False)`, keep
the name.
*Security:* tightening only (B ⊂ A). *Functional:* none unless a non-`admin` id-1
account exists. *Risk:* low-medium — `isAdmin` must be populated on every path
that reaches these functions, including MCP and assistant flows, which set it
separately (`mcp.py:67`). **Needs verification that `isAdmin` is never silently
`False` for the real admin**, or the admin starts getting filtered.
**Prerequisite: audit every construction site of `UserInfoDTO`/`BaseUserDTO`.**

### Option 3 — Both, as two commits
Option 1, then Option 2. Recommended if the bypass is to be closed.

### Option 4 — Do nothing, document
Record the id-1 bypass as accepted single-tenant behaviour and the fanout
restriction as intentional. Cheapest; leaves `important.md` inaccurate.

## 9. Recommendation

**Option 1 now, Option 2 after the `UserInfoDTO` construction audit.**

Option 1 is low-risk, restores a documented feature, and does not touch the
permission boundary. Option 2 touches the boundary and its safety depends
entirely on `isAdmin` being reliably populated — which is exactly the kind of
thing that should be verified, not assumed, before merging.

**Both remain blocked pending your explicit approval.**

## 10. Tests required before either lands

| Option | Test |
|---|---|
| 1 | Non-admin user, no row rules, two co-relevant datasources ⇒ `decompose_question` returns `mode != 'single'` |
| 1 | Non-admin user **with** a row rule ⇒ returns `single` (safety property preserved) |
| 2 | `isAdmin=True` user ⇒ `get_row_permission_filters` returns `[]` |
| 2 | `id==1, account!='admin'` ⇒ filters **are** applied (the bug being fixed) |
| 2 | `isAdmin` populated on MCP, assistant and embedded paths (integration) |
