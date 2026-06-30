---
name: review-modal-close-refresh
description: Person detail page full-data refresh when review case modal closes after an action
metadata:
  type: project
---

# Review Modal Close — Person Detail Page Refresh

## Problem

On the person detail page (`/persons/[personId]`), opening a review case in the `ReviewCaseDetailModal` and taking an action (merge approve, no-match, assign, defer, etc.) leaves the page showing stale data. The golden profile, status, connection count, and all detail tabs reflect pre-action state until the user manually navigates away and back.

A review action can trigger a merge, which may:
- Change the person's golden profile fields (name, phone, DOB, etc.)
- Change the person's status to `merged`
- Absorb this person into another (different `person_id` returned)
- Change connection count and identifier list

## Solution

Track whether any action was taken during a modal session. On modal close, if an action was taken, re-run the page's full data loading effect — which shows the loading skeleton, re-fetches all person data and detail tabs, and redirects if the person was absorbed.

## Files Changed

| File | Change |
|---|---|
| `services/frontend2/src/app/review/[reviewCaseId]/ReviewCaseDetailModal.tsx` | Track `actioned` ref; change `onClose` signature |
| `services/frontend2/src/app/persons/[personId]/page.tsx` | Add `personRefreshKey` state; update `onClose` handler |

## Design

### ReviewCaseDetailModal changes

**`actioned` ref:** `const actioned = useRef(false)` — tracks whether any action fired during this modal open.

**Reset on open:** In the existing `useEffect` that watches `open`, reset `actioned.current = false` when `open` is `true` (alongside the existing `queueMicrotask(loadDetail)`).

**`handleChanged`:** Wrap the existing `loadDetail` call passed as `onChanged` into a local `handleChanged`:
```typescript
const handleChanged = useCallback((): void => {
  actioned.current = true;
  loadDetail();
}, [loadDetail]);
```
Pass `handleChanged` as `onChanged` to `ReviewCaseDetailContent`.

**`onClose` signature change:** `onClose: () => void` → `onClose: (actioned: boolean) => void`.

All three close paths call `onClose(actioned.current)`:
- Backdrop `onClick`
- X button `onClick`
- Escape key handler

### Person detail page changes

**`personRefreshKey` state:**
```typescript
const [personRefreshKey, setPersonRefreshKey] = useState(0);
```
Added near other page-level state declarations. Included in the `useEffect` dependency array that runs `loadPersonDetail`.

**Updated `onClose` handler:**
```tsx
onClose={(wasActioned) => {
  setViewingReviewCaseId(null);
  reloadRecommendedReviewCases();
  if (wasActioned) setPersonRefreshKey((k) => k + 1);
}}
```
Incrementing `personRefreshKey` re-triggers the data loading `useEffect`, which:
1. Sets `loading = true` (loading skeleton visible)
2. Re-fetches person + identifiers in parallel
3. If `personRes.person_id !== personId`, calls `router.replace(...)` (absorbed-person redirect)
4. Re-fetches source records, sales, audit, bankruptcy cases, source record facets

## Edge Cases

| Case | Handled by |
|---|---|
| Action taken, person absorbed by merge | `loadPersonDetail` redirect: `router.replace(/persons/{surviving_id})` |
| Action taken multiple times before close | `actioned.current` already `true`; `personRefreshKey` increments once on close |
| Modal opened, no action, closed | `actioned.current = false`; no refresh, no extra fetch |
| Modal opened, action taken, Escape to close | Escape handler calls `onClose(actioned.current)` — refresh fires |
| Rapid open/close without action | No refresh triggered |

## No Design Doc Needed for

- `ReviewCaseDetailModal` used on the standalone review page (`/review/[reviewCaseId]/page.tsx`) — that page imports `ReviewCaseDetailContent` directly, not `ReviewCaseDetailModal`, so the `onClose` signature change has no effect there.
