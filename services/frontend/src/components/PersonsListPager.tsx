"use client";

import { useEffect, useState, type ReactElement } from "react";

import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import FirstPageIcon from "@mui/icons-material/FirstPage";
import LastPageIcon from "@mui/icons-material/LastPage";
import NavigateBeforeIcon from "@mui/icons-material/NavigateBefore";
import NavigateNextIcon from "@mui/icons-material/NavigateNext";

interface PersonsListPagerProps {
  firstRow: number;
  lastRow: number;
  totalCount: number;
  pageIndex: number;
  totalPages: number;
  rowsPerPage: number;
  rowsPerPageOptions: readonly number[];
  loading: boolean;
  onGoTo: (page: number) => void;
  onRowsPerPageChange: (n: number) => void;
}

export default function PersonsListPager({
  firstRow,
  lastRow,
  totalCount,
  pageIndex,
  totalPages,
  rowsPerPage,
  rowsPerPageOptions,
  loading,
  onGoTo,
  onRowsPerPageChange,
}: PersonsListPagerProps): ReactElement {
  const [pageInput, setPageInput] = useState<string>(String(pageIndex + 1));

  useEffect(() => {
    setPageInput(String(pageIndex + 1));
  }, [pageIndex]);

  function commitPageInput(): void {
    const parsed = parseInt(pageInput, 10);
    if (Number.isNaN(parsed)) {
      setPageInput(String(pageIndex + 1));
      return;
    }
    onGoTo(parsed - 1);
  }

  const atFirst: boolean = pageIndex === 0 || loading;
  const atLast: boolean = pageIndex >= totalPages - 1 || loading;

  return (
    <Stack
      direction="row"
      alignItems="center"
      spacing={1.5}
      justifyContent="flex-end"
      sx={{ px: 1, flexWrap: "wrap" }}
      useFlexGap
    >
      <Typography variant="caption" color="text.secondary">
        Rows per page
      </Typography>
      <TextField
        select
        size="small"
        value={String(rowsPerPage)}
        onChange={(e) => onRowsPerPageChange(parseInt(e.target.value, 10))}
        sx={{ width: 80 }}
      >
        {rowsPerPageOptions.map((n) => (
          <MenuItem key={n} value={String(n)}>
            {n}
          </MenuItem>
        ))}
      </TextField>
      <Typography variant="caption" color="text.secondary">
        {totalCount > 0
          ? `${firstRow.toLocaleString()}\u2013${lastRow.toLocaleString()} of ${totalCount.toLocaleString()}`
          : "0 rows"}
      </Typography>
      <Stack direction="row" spacing={0}>
        <PageIconButton
          tooltip="First page"
          disabled={atFirst}
          onClick={() => onGoTo(0)}
          icon={<FirstPageIcon fontSize="small" />}
        />
        <PageIconButton
          tooltip="Previous page"
          disabled={atFirst}
          onClick={() => onGoTo(pageIndex - 1)}
          icon={<NavigateBeforeIcon fontSize="small" />}
        />
      </Stack>
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <Typography variant="caption">Page</Typography>
        <TextField
          size="small"
          value={pageInput}
          onChange={(e) => setPageInput(e.target.value)}
          onBlur={commitPageInput}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitPageInput();
            }
          }}
          sx={{ width: 64 }}
          inputProps={{ style: { textAlign: "center" } }}
        />
        <Typography variant="caption" color="text.secondary">
          of {totalPages.toLocaleString()}
        </Typography>
      </Stack>
      <Stack direction="row" spacing={0}>
        <PageIconButton
          tooltip="Next page"
          disabled={atLast}
          onClick={() => onGoTo(pageIndex + 1)}
          icon={<NavigateNextIcon fontSize="small" />}
        />
        <PageIconButton
          tooltip="Last page"
          disabled={atLast}
          onClick={() => onGoTo(totalPages - 1)}
          icon={<LastPageIcon fontSize="small" />}
        />
      </Stack>
    </Stack>
  );
}

interface PageIconButtonProps {
  tooltip: string;
  disabled: boolean;
  onClick: () => void;
  icon: ReactElement;
}

function PageIconButton({ tooltip, disabled, onClick, icon }: PageIconButtonProps): ReactElement {
  return (
    <Tooltip title={tooltip}>
      <span>
        <Button onClick={onClick} disabled={disabled} sx={{ minWidth: 0, px: 1 }}>
          {icon}
        </Button>
      </span>
    </Tooltip>
  );
}
