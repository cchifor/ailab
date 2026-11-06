/** Parses CSV text with a header line into an array of row objects. See TASK.md rule 1. */
export function parseCsv(text) {
  const lines = text.split('\n');
  const header = lines[0].split(',').map((h) => h.trim());
  const rows = [];
  for (const line of lines.slice(1)) {
    const fields = line.split(',').map((f) => f.trim());
    const row = {};
    header.forEach((h, i) => { row[h] = fields[i]; });
    rows.push(row);
  }
  return rows;
}
