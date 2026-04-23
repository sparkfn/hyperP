"use client";

import type { ReactElement } from "react";

import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

interface PaginationBarProps {
  from: number;
  to: number;
  total: number | null;
  hasPrev: boolean;
  hasNext: boolean;
  loading: boolean;
  onPrev: () => void;
  onNext: () => void;
}

export default function PaginationBar({
  from,
  to,
  total,
  hasPrev,
  hasNext,
  loading,
  onPrev,
  onNext,
}: PaginationBarProps): ReactElement {
  const label =
    from > 0
      ? total !== null
        ? `${from}–${to} of ${total}`
        : `${from}–${to}`
      : "—";

  return (
    <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end" sx={{ mt: 1 }}>
      <Button size="small" onClick={onPrev} disabled={!hasPrev || loading}>
        ← Prev
      </Button>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Button size="small" onClick={onNext} disabled={!hasNext || loading}>
        Next →
      </Button>
    </Stack>
  );
}
