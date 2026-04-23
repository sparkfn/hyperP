"use client";

import type { ReactElement } from "react";

import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";

interface Props {
  title: string;
  children: ReactElement;
}

export default function PersonSection({ title, children }: Props): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        {title}
      </Typography>
      {children}
    </Paper>
  );
}
