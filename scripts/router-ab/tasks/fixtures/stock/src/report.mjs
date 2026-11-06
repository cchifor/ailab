/** Returns the skus at or below the threshold, lowest quantity first. See TASK.md rule 3. */
export function lowStock(stock, threshold) {
  return [...stock.entries()].filter(([, q]) => q < threshold).sort((a, b) => a[0].localeCompare(b[0])).map(([s]) => s);
}
