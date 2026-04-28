"use client";

import { useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Collapse from "@mui/material/Collapse";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";

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

function OrderRow({ order }: { order: SalesOrder }): ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const hasItems = order.line_items.length > 0;

  return (
    <>
      <TableRow
        hover
        sx={{ cursor: hasItems ? "pointer" : "default" }}
        onClick={() => { if (hasItems) setOpen(!open); }}
      >
        <TableCell sx={{ width: 40 }}>
          {hasItems ? (
            <IconButton size="small">
              {open ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
            </IconButton>
          ) : null}
        </TableCell>
        <TableCell>{order.order_no ?? order.source_order_id ?? "—"}</TableCell>
        <TableCell>{formatDate(order.order_date)}</TableCell>
        <TableCell>{formatDate(order.release_date)}</TableCell>
        <TableCell>{order.entity_name ?? order.source_system ?? "—"}</TableCell>
        <TableCell align="right">{formatCurrency(order.total_amount, order.currency)}</TableCell>
        <TableCell align="right">{order.line_items.length} items</TableCell>
      </TableRow>
      {hasItems ? (
        <TableRow>
          <TableCell colSpan={7} sx={{ py: 0, borderBottom: open ? undefined : "none" }}>
            <Collapse in={open} timeout="auto" unmountOnExit>
              <Box sx={{ py: 1, pl: 4 }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Line #</TableCell>
                      <TableCell>Product</TableCell>
                      <TableCell>Category</TableCell>
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
                        <TableCell>
                          {li.product?.category ? (
                            <Typography variant="body2" color="text.secondary" fontSize="0.75rem">
                              {li.product.category}
                            </Typography>
                          ) : "—"}
                        </TableCell>
                        <TableCell>{li.product?.sku ?? "—"}</TableCell>
                        <TableCell align="right">{li.quantity ?? "—"}</TableCell>
                        <TableCell align="right">
                          {li.unit_price !== null ? li.unit_price.toFixed(2) : "—"}
                        </TableCell>
                        <TableCell align="right">
                          {li.subtotal !== null ? li.subtotal.toFixed(2) : "—"}
                        </TableCell>
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

interface Props {
  sales: SalesOrder[];
}

export default function PublicSalesTable({ sales }: Props): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Sales
      </Typography>
      {sales.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No sales orders found.
        </Typography>
      ) : (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell />
                <TableCell>Order #</TableCell>
                <TableCell>Ordered</TableCell>
                <TableCell>Released</TableCell>
                <TableCell>Source</TableCell>
                <TableCell align="right">Total</TableCell>
                <TableCell align="right">Items</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sales.map((o, idx) => (
                <OrderRow key={o.order_no ?? o.source_order_id ?? idx} order={o} />
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}
