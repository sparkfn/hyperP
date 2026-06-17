# Review Modal Close — Person Detail Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After taking any action in the review case modal on the person detail page, closing the modal triggers a full data reload of the page (including absorbed-person redirect).

**Architecture:** Track an `actioned` ref inside `ReviewCaseDetailModal` — set to `true` whenever `onChanged` fires. Change `onClose` to pass that flag to the caller. In the person detail page, a `personRefreshKey` counter is included in the data-loading `useEffect` deps; incrementing it re-runs the full `loadPersonDetail`, which handles the absorbed-person redirect automatically.

**Tech Stack:** Next.js 15 App Router, React, TypeScript strict

---

## Files

| Action | File |
|---|---|
| Modify | `services/frontend2/src/app/review/[reviewCaseId]/ReviewCaseDetailModal.tsx` |
| Modify | `services/frontend2/src/app/persons/[personId]/page.tsx` |

---

### Task 1: Update ReviewCaseDetailModal — track actioned, change onClose signature

**Files:**
- Modify: `services/frontend2/src/app/review/[reviewCaseId]/ReviewCaseDetailModal.tsx`

- [ ] **Step 1: Add `useRef` to the React import**

Line 3 currently reads:
```typescript
import { useCallback, useEffect, useState, type ReactElement } from "react";
```
Change to:
```typescript
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
```

- [ ] **Step 2: Change the `onClose` prop type in the `ReviewCaseDetailModal` props destructure**

Locate the props block (around line 227):
```typescript
export function ReviewCaseDetailModal({
  open,
  reviewCaseId,
  onClose,
}: {
  open: boolean;
  reviewCaseId: string;
  onClose: () => void;
}): ReactElement | null {
```
Change `onClose: () => void` to `onClose: (actioned: boolean) => void`:
```typescript
export function ReviewCaseDetailModal({
  open,
  reviewCaseId,
  onClose,
}: {
  open: boolean;
  reviewCaseId: string;
  onClose: (actioned: boolean) => void;
}): ReactElement | null {
```

- [ ] **Step 3: Add `actioned` ref and `handleChanged` callback inside the component body**

After the existing `const [loading, setLoading] = useState<boolean>(true);` line, add:
```typescript
const actioned = useRef(false);
```

After the existing `loadDetail` `useCallback` declaration, add:
```typescript
const handleChanged = useCallback((): void => {
  actioned.current = true;
  loadDetail();
}, [loadDetail]);
```

- [ ] **Step 4: Reset `actioned.current` when the modal opens**

Find the `useEffect` that watches `open` (currently calls `queueMicrotask(loadDetail)`):
```typescript
useEffect(() => {
  if (!open) {
    setDetail(null);
    setError(null);
    return;
  }
  queueMicrotask(loadDetail);
}, [open, loadDetail]);
```
Add `actioned.current = false;` before `queueMicrotask`:
```typescript
useEffect(() => {
  if (!open) {
    setDetail(null);
    setError(null);
    return;
  }
  actioned.current = false;
  queueMicrotask(loadDetail);
}, [open, loadDetail]);
```

- [ ] **Step 5: Update the Escape key handler to pass `actioned.current`**

Find the second `useEffect` (keyboard handler):
```typescript
useEffect(() => {
  if (!open) return;
  const onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape") onClose();
  };
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}, [open, onClose]);
```
Change `onClose()` to `onClose(actioned.current)`:
```typescript
useEffect(() => {
  if (!open) return;
  const onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape") onClose(actioned.current);
  };
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}, [open, onClose]);
```

- [ ] **Step 6: Update the backdrop and X button onClick handlers**

Find the returned JSX. The backdrop div currently has `onClick={onClose}` and the X button has `onClick={onClose}`. Change both:

```tsx
return (
  <div className={styles.reviewCaseOverlay} onClick={() => onClose(actioned.current)}>
    <div className={styles.reviewCaseModal} onClick={(e) => e.stopPropagation()}>
      <div className={styles.reviewCaseModalHeader}>
        <Link href={`/review/${encodeURIComponent(reviewCaseId)}`} className={styles.reviewCaseOpenFull} target="_blank" rel="noopener noreferrer">
          Open in full page ↗
        </Link>
        <button type="button" className={styles.reviewCaseModalClose} onClick={() => onClose(actioned.current)} aria-label="Close">×</button>
      </div>
      {loading && detail === null ? (
        <div className={styles.reviewCaseModalLoading}>Loading review case…</div>
      ) : error !== null && detail === null ? (
        <div className={styles.reviewCaseModalError}>{error}</div>
      ) : detail !== null ? (
        <ReviewCaseDetailContent detail={detail} loading={loading} error={error} onChanged={handleChanged} />
      ) : null}
    </div>
  </div>
);
```

Note the two changes in the JSX:
1. `onClick={onClose}` on the overlay div → `onClick={() => onClose(actioned.current)}`
2. `onClick={onClose}` on the close button → `onClick={() => onClose(actioned.current)}`
3. `onChanged={loadDetail}` on `ReviewCaseDetailContent` → `onChanged={handleChanged}`

- [ ] **Step 7: Run typecheck on the modal file**

```bash
cd services/frontend2 && npm run typecheck 2>&1 | head -40
```
Expected: no new errors. If you see `onClose` type errors, verify Steps 2 and 5–6 were applied correctly.

- [ ] **Step 8: Commit**

```bash
git add services/frontend2/src/app/review/[reviewCaseId]/ReviewCaseDetailModal.tsx
git commit -m "feat(frontend2): track actioned flag in ReviewCaseDetailModal, pass to onClose"
```

---

### Task 2: Update person detail page — personRefreshKey triggers full reload

**Files:**
- Modify: `services/frontend2/src/app/persons/[personId]/page.tsx`

- [ ] **Step 1: Add `personRefreshKey` state**

Find the block of page-level state declarations (around line 3465–3470, near `const [person, setPerson] = useState<Person | null>(null)`):
```typescript
const [person, setPerson] = useState<Person | null>(null);
const [detailData, setDetailData] = useState<DetailData>(EMPTY_DETAIL);
const [tabTotals, setTabTotals] = useState<Partial<Record<Tab, number>>>({});
const [loading, setLoading] = useState(true);
const pageLoadId = useId();
```
Add `personRefreshKey` after `loading`:
```typescript
const [person, setPerson] = useState<Person | null>(null);
const [detailData, setDetailData] = useState<DetailData>(EMPTY_DETAIL);
const [tabTotals, setTabTotals] = useState<Partial<Record<Tab, number>>>({});
const [loading, setLoading] = useState(true);
const [personRefreshKey, setPersonRefreshKey] = useState(0);
const pageLoadId = useId();
```

- [ ] **Step 2: Add `personRefreshKey` to the data-loading `useEffect` dependency array**

Find the dependency array at the end of the `useEffect` that runs `loadPersonDetail` (currently line ~3561):
```typescript
}, [pageLoadId, personId, setGlobalLoading]);
```
Change to:
```typescript
}, [pageLoadId, personId, setGlobalLoading, personRefreshKey]);
```

- [ ] **Step 3: Update the `ReviewCaseDetailModal` `onClose` handler**

Find the modal usage (around line 2202–2209):
```tsx
<ReviewCaseDetailModal
  open={viewingReviewCaseId !== null}
  reviewCaseId={viewingReviewCaseId ?? ""}
  onClose={() => {
    setViewingReviewCaseId(null);
    reloadRecommendedReviewCases();
  }}
/>
```
Change to:
```tsx
<ReviewCaseDetailModal
  open={viewingReviewCaseId !== null}
  reviewCaseId={viewingReviewCaseId ?? ""}
  onClose={(wasActioned) => {
    setViewingReviewCaseId(null);
    reloadRecommendedReviewCases();
    if (wasActioned) setPersonRefreshKey((k) => k + 1);
  }}
/>
```

- [ ] **Step 4: Run typecheck**

```bash
cd services/frontend2 && npm run typecheck 2>&1 | head -40
```
Expected: clean. If you see a type error on `onClose`, confirm the `ReviewCaseDetailModal` prop type was updated in Task 1 Step 2.

- [ ] **Step 5: Run lint**

```bash
cd services/frontend2 && npm run lint 2>&1 | tail -20
```
Expected: warning count should not increase relative to the pre-existing baseline (~18 warnings). Zero new warnings from your changes.

- [ ] **Step 6: Commit**

```bash
git add services/frontend2/src/app/persons/[personId]/page.tsx
git commit -m "feat(frontend2): refresh person detail page when review modal closes after action"
```

---

### Task 3: Manual verification

- [ ] **Step 1: Rebuild and restart frontend2**

```bash
docker compose build --no-cache frontend2 && docker compose up -d frontend2
```
Wait for the container to be healthy (`docker compose logs -f frontend2` — look for `ready` log line).

- [ ] **Step 2: Verify — action taken, modal closes, page refreshes**

1. Open a person detail page that has an open review case in the Matches tab.
2. Click the view button to open the `ReviewCaseDetailModal`.
3. Take any action (e.g., assign, defer, or approve merge).
4. Close the modal via the X button, backdrop click, or Escape.
5. Observe: the page loading skeleton appears briefly, then the golden profile and all detail sections reload with fresh data.

- [ ] **Step 3: Verify — view only, modal closes, page does NOT reload**

1. Open the same modal without taking any action.
2. Close it.
3. Observe: the page does NOT show the loading skeleton — no refresh occurs.

- [ ] **Step 4: Verify — absorbed-person redirect**

If a test case exists where a merge action absorbs the current person:
1. Open a review case modal for a person pair where this person would be absorbed.
2. Approve the merge.
3. Close the modal.
4. Observe: the browser redirects to the surviving person's profile page.

(If no such test case is available, this path is covered by the existing `loadPersonDetail` logic which already handles it — the redirect at line ~3508.)
