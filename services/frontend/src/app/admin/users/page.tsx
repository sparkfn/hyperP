"use client";

import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { bffFetch, BffError } from "@/lib/api-client";
import type { UserResponse } from "@/lib/api-types-ops";
import type { Role } from "@/lib/permissions";
import { isRole } from "@/lib/permissions";

type UserRow = UserResponse;

interface EntityRow {
  entity_key: string;
  display_name: string | null;
}

const ROLE_OPTIONS: readonly Role[] = ["admin", "employee", "first_time"] as const;

export default function UsersAdminPage(): ReactElement {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [entities, setEntities] = useState<EntityRow[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const loadUsers = useCallback(async (): Promise<void> => {
    try {
      setLoading(true);
      const rows = await bffFetch<UserRow[]>("/bff/users");
      setUsers(rows);
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Failed to load users");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEntities = useCallback(async (): Promise<void> => {
    try {
      const rows = await bffFetch<EntityRow[]>("/bff/entities");
      setEntities(rows);
    } catch {
      // non-fatal; entity selector just won't suggest.
    }
  }, []);

  useEffect(() => {
    void loadUsers();
    void loadEntities();
  }, [loadUsers, loadEntities]);

  const entityOptions: readonly EntityRow[] = useMemo(
    () => [...entities].sort((a, b) => a.entity_key.localeCompare(b.entity_key)),
    [entities],
  );

  const patchUser = useCallback(async (email: string, updates: { role?: Role; entity_key?: string | null }): Promise<void> => {
    setBusy(email);
    setErr(null);
    try {
      await bffFetch<UserRow>(`/bff/users/${encodeURIComponent(email)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(updates),
      });
      await loadUsers();
    } catch (e: unknown) {
      setErr(e instanceof BffError ? e.message : "Update failed");
    } finally {
      setBusy(null);
    }
  }, [loadUsers]);

  return (
    <Stack spacing={2}>
      <Typography variant="h5" fontWeight={700}>
        User management
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Assign roles and tenants. First-time users are blocked until assigned.
      </Typography>
      {err ? <Alert severity="error">{err}</Alert> : null}
      <Paper>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Email</TableCell>
              <TableCell>Name</TableCell>
              <TableCell>Role</TableCell>
              <TableCell>Entity</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={5}>Loading…</TableCell>
              </TableRow>
            ) : users.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5}>No users yet.</TableCell>
              </TableRow>
            ) : (
              users.map((u) => (
                <UserRowEditor
                  key={u.email}
                  user={u}
                  entities={entityOptions}
                  busy={busy === u.email}
                  onSave={patchUser}
                />
              ))
            )}
          </TableBody>
        </Table>
      </Paper>
    </Stack>
  );
}

interface RowEditorProps {
  user: UserRow;
  entities: readonly EntityRow[];
  busy: boolean;
  onSave: (email: string, updates: { role?: Role; entity_key?: string | null }) => Promise<void>;
}

function UserRowEditor(props: RowEditorProps): ReactElement {
  const [role, setRole] = useState<Role>(props.user.role);
  const [entityKey, setEntityKey] = useState<string>(props.user.entity_key ?? "");

  const dirty: boolean =
    role !== props.user.role || entityKey !== (props.user.entity_key ?? "");

  async function save(): Promise<void> {
    const payload: { role?: Role; entity_key?: string | null } = { role };
    payload.entity_key = role === "admin" || role === "first_time" ? null : entityKey || null;
    await props.onSave(props.user.email, payload);
  }

  return (
    <TableRow>
      <TableCell>{props.user.email}</TableCell>
      <TableCell>{props.user.display_name ?? "—"}</TableCell>
      <TableCell>
        <TextField
          size="small"
          select
          value={role}
          onChange={(e) => {
            const nextRole: string = e.target.value;
            if (isRole(nextRole)) setRole(nextRole);
          }}
          sx={{ minWidth: 130 }}
        >
          {ROLE_OPTIONS.map((r) => (
            <MenuItem key={r} value={r}>
              <Chip
                size="small"
                label={r}
                color={r === "admin" ? "success" : r === "employee" ? "info" : "warning"}
              />
            </MenuItem>
          ))}
        </TextField>
      </TableCell>
      <TableCell>
        <TextField
          size="small"
          select
          value={entityKey}
          onChange={(e) => setEntityKey(e.target.value)}
          disabled={role === "admin" || role === "first_time"}
          sx={{ minWidth: 200 }}
        >
          <MenuItem value="">
            <em>None</em>
          </MenuItem>
          {props.entities.map((e) => (
            <MenuItem key={e.entity_key} value={e.entity_key}>
              {e.display_name ?? e.entity_key}
            </MenuItem>
          ))}
        </TextField>
      </TableCell>
      <TableCell align="right">
        <Box sx={{ display: "inline-block" }}>
          <Button
            size="small"
            variant="contained"
            disabled={!dirty || props.busy}
            onClick={() => void save()}
          >
            {props.busy ? "Saving…" : "Save"}
          </Button>
        </Box>
      </TableCell>
    </TableRow>
  );
}
