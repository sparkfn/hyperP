"use client";

import { useState, type MouseEvent, type ReactElement, type ReactNode } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Popover from "@mui/material/Popover";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

export interface CountCardItem {
  primary: string;
  secondary?: string | null;
  color?: "default" | "primary" | "success" | "warning" | "info";
}

interface CountCardsCellProps {
  count: number;
  label: string;
  items?: CountCardItem[];
  loading?: boolean;
  onOpen?: () => void;
  emptyText?: string;
}

export default function CountCardsCell({
  count,
  label,
  items,
  loading = false,
  onOpen,
  emptyText = "None",
}: CountCardsCellProps): ReactElement {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  function handleClick(e: MouseEvent<HTMLDivElement>): void {
    e.stopPropagation();
    if (count === 0) return;
    setAnchor(e.currentTarget);
    if (onOpen) onOpen();
  }

  const disabled: boolean = count === 0;

  return (
    <>
      <Box
        onClick={handleClick}
        role={disabled ? undefined : "button"}
        tabIndex={disabled ? -1 : 0}
        sx={{
          display: "inline-flex",
          alignItems: "center",
          gap: 0.5,
          px: 0.75,
          py: 0.25,
          borderRadius: 1,
          cursor: disabled ? "default" : "pointer",
          border: "1px solid",
          borderColor: disabled ? "rgba(15,23,42,0.08)" : "rgba(31,78,158,0.25)",
          bgcolor: disabled ? "transparent" : "rgba(31,78,158,0.04)",
          color: disabled ? "text.disabled" : "primary.main",
          fontSize: "0.75rem",
          fontWeight: 600,
          lineHeight: 1.2,
          "&:hover": disabled ? {} : { bgcolor: "rgba(31,78,158,0.1)" },
        }}
      >
        <span>{count}</span>
        <Typography component="span" variant="caption" sx={{ color: "inherit", fontWeight: 500 }}>
          {label}
        </Typography>
      </Box>
      <Popover
        open={anchor !== null}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        onClick={(e) => e.stopPropagation()}
      >
        <Box sx={{ minWidth: 240, maxWidth: 360, p: 1.25 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>
            {label} ({count})
          </Typography>
          {loading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 1 }}>
              <CircularProgress size={18} />
            </Box>
          ) : !items || items.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {emptyText}
            </Typography>
          ) : (
            <PopoverList items={items} />
          )}
        </Box>
      </Popover>
    </>
  );
}

function PopoverList({ items }: { items: CountCardItem[] }): ReactElement {
  const rows: ReactNode[] = items.map((item, idx) => (
    <Stack
      key={`${item.primary}-${idx}`}
      direction="row"
      alignItems="center"
      spacing={1}
      sx={{ py: 0.25 }}
    >
      <Chip
        label={item.primary}
        size="small"
        color={item.color ?? "default"}
        variant="outlined"
        sx={{ maxWidth: "100%" }}
      />
      {item.secondary ? (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
        >
          {item.secondary}
        </Typography>
      ) : null}
    </Stack>
  ));
  return <Stack spacing={0}>{rows}</Stack>;
}
