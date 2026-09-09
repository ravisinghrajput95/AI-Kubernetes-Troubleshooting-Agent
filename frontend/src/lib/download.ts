/**
 * Handing a file to the person using the console.
 *
 * One place, because there are two callers with different sources — a report
 * fetched from the API and a manifest built in the browser — and a second
 * implementation of the object-URL dance is a second place to forget
 * `revokeObjectURL`.
 */
export function saveBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

/**
 * The filename the server asked for, out of its `Content-Disposition`.
 *
 * The platform names every report `investigation-<id>.<ext>`, and taking that
 * rather than composing it here keeps one namer. Falls back rather than
 * throwing: a proxy that strips the header should cost the suggested name, not
 * the download.
 */
export function filenameFrom(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  return match?.[1] ?? fallback;
}
