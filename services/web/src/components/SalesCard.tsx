"use client";

import { useEffect, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";

import { bffFetch } from "@/lib/api-client";
import type { SalesOrder } from "@/lib/api-types";

function formatCurrency(amount: number | null, currency: string | null): string {
  if (amount === null) return "—";
  const prefix = currency ? `${currency} ` : "";
  return `${prefix}${amount.toFixed(2)}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString();
  } catch {
    return iso;
  }
}

interface OrderRowProps {
  order: SalesOrder;
}

function OrderRow({ order }: OrderRowProps): ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const hasItems = order.line_items.length > 0;

  return (
    <>
      <TableRow hover sx={{ cursor: hasItems ? "pointer" : "default" }} onClick={() => { if (hasItems) setOpen(!open); }}>
        <TableCell sx={{ width: 40 }}>
          {hasItems ? (
            <IconButton size="small">
              {open ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
            </IconButton>
          ) : null}
        </TableCell>
        <TableCell>{order.order_no ?? order.source_order_id ?? "—"}</TableCell>
        <TableCell>{formatDate(order.order_date)}</TableCell>
        <TableCell>{order.entity_name ?? order.source_system ?? "—"}</TableCell>
        <TableCell align="right">{formatCurrency(order.total_amount, order.currency)}</TableCell>
        <TableCell align="right">
          <Chip label={`${order.line_items.length} items`} size="small" variant="outlined" />
        </TableCell>
      </TableRow>
      {hasItems ? (
        <TableRow>
          <TableCell colSpan={6} sx={{ py: 0, borderBottom: open ? undefined : "none" }}>
            <Collapse in={open} timeout="auto" unmountOnExit>
              <Box sx={{ py: 1, pl: 4 }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Line #</TableCell>
                      <TableCell>Product</TableCell>
                      <TableCell>SKU</TableCell>
                      <TableCell align="right">Qty</TableCell>
                      <TableCell align="right">Unit Price</TableCell>
                      <TableCell align="right">Subtotal</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {order.line_items.map((li, idx) => (
                      <TableRow key={li.line_no ?? idx}>
                        <TableCell>{li.line_no ?? "—"}</TableCell>
                        <TableCell>{li.product?.display_name ?? "—"}</TableCell>
                        <TableCell>{li.product?.sku ?? "—"}</TableCell>
                        <TableCell align="right">{li.quantity ?? "—"}</TableCell>
                        <TableCell align="right">{li.unit_price !== null ? li.unit_price.toFixed(2) : "—"}</TableCell>
                        <TableCell align="right">{li.subtotal !== null ? li.subtotal.toFixed(2) : "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Collapse>
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

interface SalesCardProps {
  personId: string;
}

export default function SalesCard({ personId }: SalesCardProps): ReactElement {
  const [orders, setOrders] = useState<SalesOrder[] | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    async function load(): Promise<void> {
      try {
        const data = await bffFetch<SalesOrder[]>(
          `/api/persons/${encodeURIComponent(personId)}/sales`,
        );
        if (!cancelled) setOrders(data);
      } catch {
        if (!cancelled) setOrders([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [personId]);

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Typography variant="h6">Sales History</Typography>
        {loading ? <CircularProgress size={18} /> : null}
      </Stack>
      {!loading && orders !== null && orders.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No purchase history found.
        </Typography>
      ) : null}
      {orders !== null && orders.length > 0 ? (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell />
              <TableCell>Order #</TableCell>
              <TableCell>Date</TableCell>
              <TableCell>Source</TableCell>
              <TableCell align="right">Total</TableCell>
              <TableCell align="right">Items</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {orders.map((o, idx) => (
              <OrderRow key={o.order_no ?? o.source_order_id ?? idx} order={o} />
            ))}
          </TableBody>
        </Table>
      ) : null}
    </Paper>
  );
}
