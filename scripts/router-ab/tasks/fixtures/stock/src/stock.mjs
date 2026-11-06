/** Applies movement rows to a stock table (Map sku -> qty). See TASK.md rule 2. */
export function applyMovements(stock, rows) {
  const out = stock;
  rows.forEach((r, i) => {
    const qty = parseInt(r.qty, 10);
    const cur = out.get(r.sku) ?? 0;
    if (r.kind === 'in') out.set(r.sku, cur + qty);
    else out.set(r.sku, cur + qty);
  });
  return out;
}
