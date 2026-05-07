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
import type {
  UserBulkCreateRequest,
  UserBulkCreateResult,
  UserResponse,
} from "@/lib/api-types-ops";
import type { Role } from "@/lib/permissions";
import { isRole } from "@/lib/permissions";

type UserRow = UserResponse;

interface EntityRow {
  entity_key: string;
  display_name: string | null;
}

interface DraftUserRow {
  id: number;
  email: string;
  role: Role;
  entityKey: string;
  result: UserBulkCreateResult | null;
}

interface BulkEditorProps {
  entities: readonly EntityRow[];
  onComplete: () => Promise<void>;
  onUpdateExisting: (email: string, updates: { role?: Role; entity_key?: string | null }) => Promise<void>;
}

const ROLE_OPTIONS: readonly Role[] = ["admin", "employee", "first_time"] as const;
const INITIAL_DRAFT_ROW: DraftUserRow = {
  id: 1,
  email: "",
  role: "employee",
  entityKey: "",
  result: null,
};

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
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadUsers();
    void loadEntities();
  }, [loadUsers, loadEntities]);

  const entityOptions: readonly EntityRow[] = useMemo(
    () => [...entities].sort((a, b) => a.entity_key.localeCompare(b.entity_key)),
    [entities],
  );

  const patchUser = useCallback(
    async (email: string, updates: { role?: Role; entity_key?: string | null }): Promise<void> => {
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
        throw e;
      } finally {
        setBusy(null);
      }
    },
    [loadUsers],
  );

  return (
    <Stack spacing={2}>
      <Typography variant="h5" fontWeight={700}>
        User management
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Assign roles and tenants. First-time users are blocked until assigned.
      </Typography>
      {err ? <Alert severity="error">{err}</Alert> : null}
      <BulkUserEditor entities={entityOptions} onComplete={loadUsers} onUpdateExisting={patchUser} />
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
                  key={`${u.email}:${u.role}:${u.entity_key ?? ""}`}
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

function BulkUserEditor(props: BulkEditorProps): ReactElement {
  const [rows, setRows] = useState<DraftUserRow[]>([INITIAL_DRAFT_ROW]);
  const [nextRowId, setNextRowId] = useState<number>(2);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [updatingRowId, setUpdatingRowId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const formBusy: boolean = submitting || updatingRowId !== null;

  function updateRow(rowId: number, changes: Partial<Omit<DraftUserRow, "id">>): void {
    setRows((current) =>
      current.map((row) => (row.id === rowId ? { ...row, ...changes } : row)),
    );
  }

  function addRow(): void {
    const newRow: DraftUserRow = {
      id: nextRowId,
      email: "",
      role: "employee",
      entityKey: "",
      result: null,
    };
    setRows((current) => [...current, newRow]);
    setNextRowId((current) => current + 1);
  }

  function removeRow(rowId: number): void {
    setRows((current) => current.filter((row) => row.id !== rowId));
  }

  async function submitRows(): Promise<void> {
    setSubmitting(true);
    setError(null);
    const submittedRows: DraftUserRow[] = rows;
    const request: UserBulkCreateRequest = {
      users: submittedRows.map((row) => ({
        email: row.email.trim(),
        role: row.role,
        entity_key: row.role === "employee" ? row.entityKey || null : null,
      })),
    };

    try {
      const response = await bffFetch<{ results: UserBulkCreateResult[] }>("/bff/users/bulk", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
      });
      const resultByRowId: Map<number, UserBulkCreateResult> = new Map();
      submittedRows.forEach((row, index) => {
        const result: UserBulkCreateResult | undefined = response.results[index];
        if (result !== undefined) {
          resultByRowId.set(row.id, result);
        }
      });
      setRows((current) =>
        current.map((row) => ({
          ...row,
          result: resultByRowId.get(row.id) ?? row.result,
        })),
      );
      await props.onComplete();
    } catch (e: unknown) {
      setError(e instanceof BffError ? e.message : "Bulk user creation failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function updateExisting(row: DraftUserRow): Promise<void> {
    setUpdatingRowId(row.id);
    setError(null);
    try {
      await props.onUpdateExisting(row.email.trim(), {
        role: row.role,
        entity_key: row.role === "employee" ? row.entityKey || null : null,
      });
      const updatedResult: UserBulkCreateResult = {
        email: row.email.trim(),
        status: "created",
        code: null,
        message: "Existing user updated.",
        user: null,
      };
      updateRow(row.id, { result: updatedResult });
      await props.onComplete();
    } catch (e: unknown) {
      setError(e instanceof BffError ? e.message : "Existing user update failed");
    } finally {
      setUpdatingRowId(null);
    }
  }

  return (
    <Paper sx={{ p: 2 }}>
      <Stack spacing={2}>
        <Box>
          <Typography variant="h6" fontWeight={700}>
            Bulk add users
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Pre-register users by email, role, and entity before their first Google sign-in.
          </Typography>
        </Box>
        {error ? <Alert severity="error">{error}</Alert> : null}
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Email</TableCell>
              <TableCell>Role</TableCell>
              <TableCell>Entity</TableCell>
              <TableCell>Status</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.id}>
                <TableCell>
                  <TextField
                    size="small"
                    label="Email"
                    value={row.email}
                    onChange={(e) => updateRow(row.id, { email: e.target.value, result: null })}
                    placeholder="user@example.com"
                    disabled={formBusy}
                    sx={{ minWidth: 240 }}
                  />
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    label="Role"
                    select
                    value={row.role}
                    disabled={formBusy}
                    onChange={(e) => {
                      const nextRole: string = e.target.value;
                      if (isRole(nextRole)) {
                        updateRow(row.id, {
                          role: nextRole,
                          entityKey: nextRole === "employee" ? row.entityKey : "",
                          result: null,
                        });
                      }
                    }}
                    sx={{ minWidth: 130 }}
                  >
                    {ROLE_OPTIONS.map((roleOption) => (
                      <MenuItem key={roleOption} value={roleOption}>
                        <Chip
                          size="small"
                          label={roleOption}
                          color={
                            roleOption === "admin"
                              ? "success"
                              : roleOption === "employee"
                                ? "info"
                                : "warning"
                          }
                        />
                      </MenuItem>
                    ))}
                  </TextField>
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    label="Entity"
                    select
                    value={row.entityKey}
                    onChange={(e) => updateRow(row.id, { entityKey: e.target.value, result: null })}
                    disabled={formBusy || row.role !== "employee"}
                    sx={{ minWidth: 200 }}
                  >
                    <MenuItem value="">
                      <em>None</em>
                    </MenuItem>
                    {props.entities.map((entity) => (
                      <MenuItem key={entity.entity_key} value={entity.entity_key}>
                        {entity.display_name ?? entity.entity_key}
                      </MenuItem>
                    ))}
                  </TextField>
                </TableCell>
                <TableCell>
                  <Stack spacing={1} direction="row" alignItems="center">
                    {renderBulkStatus(row.result)}
                    {row.result?.code === "user_exists" ? (
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={formBusy}
                        onClick={() => {
                          void updateExisting(row);
                        }}
                      >
                        {updatingRowId === row.id ? "Updating…" : "Update existing user"}
                      </Button>
                    ) : null}
                  </Stack>
                </TableCell>
                <TableCell align="right">
                  <Button
                    size="small"
                    variant="outlined"
                    disabled={rows.length === 1 || formBusy}
                    onClick={() => removeRow(row.id)}
                  >
                    Remove
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <Button
            size="small"
            variant="outlined"
            disabled={formBusy}
            onClick={addRow}
          >
            Add row
          </Button>
          <Button
            size="small"
            variant="contained"
            disabled={formBusy}
            onClick={() => {
              void submitRows();
            }}
          >
            {submitting ? "Submitting…" : "Submit bulk users"}
          </Button>
        </Stack>
      </Stack>
    </Paper>
  );
}

function renderBulkStatus(result: UserBulkCreateResult | null): ReactElement {
  if (result === null) {
    return <Box component="span">—</Box>;
  }
  return (
    <Chip
      size="small"
      color={result.status === "created" ? "success" : "error"}
      label={result.message ?? (result.status === "created" ? "Created" : "Error")}
    />
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

  const dirty: boolean = role !== props.user.role || entityKey !== (props.user.entity_key ?? "");

  async function save(): Promise<void> {
    const payload: { role?: Role; entity_key?: string | null } = { role };
    payload.entity_key = role === "admin" || role === "first_time" ? null : entityKey || null;
    try {
      await props.onSave(props.user.email, payload);
    } catch {
      // Parent handler displays the error alert.
    }
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
