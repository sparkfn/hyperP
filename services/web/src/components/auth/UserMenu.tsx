"use client";

import { useState, type ReactElement, type MouseEvent } from "react";
import { signOut } from "next-auth/react";

import Avatar from "@mui/material/Avatar";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import ListItemText from "@mui/material/ListItemText";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import type { Role } from "@/lib/permissions";

interface UserMenuProps {
  email: string;
  displayName: string | null;
  role: Role;
  entityKey: string | null;
}

function roleColor(role: Role): "success" | "info" | "warning" {
  if (role === "admin") return "success";
  if (role === "employee") return "info";
  return "warning";
}

export default function UserMenu(props: UserMenuProps): ReactElement {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const initial: string =
    (props.displayName ?? props.email).trim().slice(0, 1).toUpperCase() || "?";

  function open(e: MouseEvent<HTMLButtonElement>): void {
    setAnchor(e.currentTarget);
  }
  function close(): void {
    setAnchor(null);
  }

  return (
    <Box sx={{ ml: 1 }}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Chip
          size="small"
          color={roleColor(props.role)}
          label={props.role === "first_time" ? "pending" : props.role}
          sx={{ textTransform: "uppercase", fontWeight: 600, letterSpacing: 0.5 }}
        />
        <IconButton size="small" onClick={open} aria-label="account menu">
          <Avatar sx={{ width: 28, height: 28, fontSize: 14 }}>{initial}</Avatar>
        </IconButton>
      </Stack>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close}>
        <MenuItem disabled>
          <ListItemText
            primary={props.displayName ?? props.email}
            secondary={
              props.entityKey ? `Entity: ${props.entityKey}` : "No entity assigned"
            }
          />
        </MenuItem>
        <Divider />
        <MenuItem
          onClick={() => {
            close();
            void signOut({ callbackUrl: "/login" });
          }}
        >
          <Typography color="error">Sign out</Typography>
        </MenuItem>
      </Menu>
    </Box>
  );
}
