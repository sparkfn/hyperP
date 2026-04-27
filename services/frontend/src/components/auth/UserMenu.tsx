"use client";

import {
  type MouseEvent,
  type ReactElement,
  useCallback,
  useEffect,
  useState,
} from "react";
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
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { Role } from "@/lib/permissions";

interface UserMenuProps {
  email: string;
  displayName: string | null;
  role: Role;
  entityKey: string | null;
  sessionError?: string;
}

export function UserMenu(props: UserMenuProps): ReactElement {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const close = useCallback(() => setAnchor(null), []);

  // When NextAuth detects a refresh-token failure it sets session.error.
  // Redirect to login immediately rather than leaving the user on a stale UI.
  useEffect(() => {
    if (props.sessionError === "RefreshTokenError") {
      void signOut({ callbackUrl: "/login", redirect: true });
    }
  }, [props.sessionError]);

  function open(event: MouseEvent<HTMLElement>): void {
    setAnchor(event.currentTarget);
  }

  const initial: string = (props.displayName ?? props.email).slice(0, 2).toUpperCase();
  return (
    <Box>
      <Stack direction="row" alignItems="center" gap={1}>
        <Chip
          label={props.role}
          size="small"
          color={props.role === "admin" ? "error" : "primary"}
          variant="outlined"
        />
        <Tooltip title="Account menu">
          <IconButton onClick={open} size="small" sx={{ p: 0 }}>
            <Avatar sx={{ width: 28, height: 28, fontSize: 14 }}>{initial}</Avatar>
          </IconButton>
        </Tooltip>
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
