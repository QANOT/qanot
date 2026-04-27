/**
 * Composition asset URL allowlist.
 *
 * Per docs/video-engine/ARCHITECTURE.md §6.3 + §7.4: the lint pass extracts
 * every external asset reference from the composition HTML and validates
 * each before any Chromium subprocess is spawned. This is the same defense
 * the Python `web_fetch` tool uses (qanot/tools/web.py:_is_ip_blocked) --
 * if a composition can fetch SSRF-inside-the-browser, an attacker who
 * controls a render request can pivot to internal services.
 *
 * Validation rules:
 *   - Schemes: http://localhost (and 127.0.0.1, ::1) | https:// |
 *     data: (max 1 MB inlined) | file: (only under FILE_ASSET_ROOTS).
 *   - Reject hostnames that resolve to RFC 1918, loopback, link-local,
 *     CGNAT, ULA, IPv4-mapped IPv6, NAT64, or the documented
 *     metadata.google.internal / *.local / *.internal patterns.
 *   - Non-HTTPS to non-loopback is rejected.
 *
 * URL extraction targets:
 *   - src= attributes  (img / video / audio / script / iframe / source)
 *   - href= attributes (a / link)
 *   - inline `style="...url(...)..."`  (background-image etc.)
 *   - <style>...</style> blocks containing url(...) refs
 *
 * The check is implemented with regex rather than a full HTML parser
 * because the lint already enforces structural validity and we want the
 * pre-render hook to be cheap (every render hits this).
 */

import { promises as dnsp } from "node:dns";
import { isIP } from "node:net";
import { resolve as resolvePath } from "node:path";

/** Per ARCHITECTURE §3.4 closed-set: this guard maps to asset_fetch_failed. */
export type AssetGuardErrorCode = "asset_fetch_failed";

/** Maximum size for an inline `data:` URL after base64 decode. */
export const MAX_DATA_URL_BYTES = 1 * 1024 * 1024;

/**
 * Whitelisted on-disk roots for `file://` references. The render service
 * only ships compositions through HTTP/data URLs in production; `file://`
 * is an escape hatch for local fonts and packaged demo assets baked into
 * the image. Anything outside these roots is rejected.
 */
const DEFAULT_FILE_ASSET_ROOTS: readonly string[] = [
  "/app/compositions",
  "/app/assets",
];

/**
 * Hostname literal/suffix denylist. Mirrors qanot/tools/web.py.
 * Everything is lower-cased before comparison.
 */
const BLOCKED_HOSTNAMES = new Set<string>([
  "metadata.google.internal",
  "metadata",
  "kubernetes.default.svc",
]);
const BLOCKED_HOSTNAME_SUFFIXES: readonly string[] = [
  ".local",
  ".internal",
  ".cluster.local",
  ".svc.cluster.local",
];

const LOOPBACK_HOSTS = new Set<string>(["localhost"]);

/**
 * Per ARCHITECTURE §7.4. We use plain CIDR strings + a tiny matcher to keep
 * the dep tree empty (no `ip-cidr` etc.). Each CIDR is pre-normalized into
 * a numeric prefix/mask pair on first use.
 */
const BLOCKED_V4_CIDRS: readonly string[] = [
  "10.0.0.0/8",
  "172.16.0.0/12",
  "192.168.0.0/16",
  "127.0.0.0/8",
  "169.254.0.0/16",
  "100.64.0.0/10",
  "0.0.0.0/8",
  "224.0.0.0/4",
  "240.0.0.0/4",
];
const BLOCKED_V6_CIDRS: readonly string[] = [
  "::1/128",
  "fe80::/10",
  "fc00::/7",
  "::ffff:0:0/96", // IPv4-mapped IPv6
  "64:ff9b::/96", // NAT64 well-known
  "2001:db8::/32",
  "ff00::/8",
];

export interface AssetGuardOptions {
  /** Whitelisted file:// roots; defaults to /app/compositions + /app/assets. */
  fileAssetRoots?: readonly string[];
  /**
   * Override DNS lookup for tests. Must return zero or more A/AAAA results
   * matching `dns.promises.lookup({all: true})`.
   */
  resolver?: (hostname: string) => Promise<ReadonlyArray<{ address: string; family: number }>>;
  /** Hard cap on bytes to inspect after base64 decode for `data:` URLs. */
  maxDataUrlBytes?: number;
}

export interface AssetGuardOk {
  ok: true;
  /** Flat list of asset URLs we inspected (deduped, in scan order). */
  urls: ReadonlyArray<string>;
}

export interface AssetGuardFailed {
  ok: false;
  code: AssetGuardErrorCode;
  message: string;
  /** First offending URL, if any (string). data: URLs are truncated. */
  offending_url?: string;
  /** Reason discriminator -- handy for tests + logs. */
  reason:
    | "blocked_scheme"
    | "blocked_hostname"
    | "blocked_ip"
    | "non_https"
    | "data_too_large"
    | "invalid_data_url"
    | "file_outside_whitelist"
    | "dns_failure"
    | "invalid_url";
}

export type AssetGuardResult = AssetGuardOk | AssetGuardFailed;

/**
 * Top-level entry point. Walks the composition HTML, extracts every
 * referenced URL, and validates each. Resolves with `ok:true` only when
 * every URL passes; the first failure short-circuits.
 */
export async function checkCompositionAssets(
  html: string,
  opts: AssetGuardOptions = {},
): Promise<AssetGuardResult> {
  const urls = extractAssetUrls(html);
  const fileRoots = (opts.fileAssetRoots ?? DEFAULT_FILE_ASSET_ROOTS).map(
    (r) => resolvePath(r),
  );
  const resolver = opts.resolver ?? defaultResolver;
  const maxDataBytes = opts.maxDataUrlBytes ?? MAX_DATA_URL_BYTES;

  const inspected: string[] = [];
  for (const url of urls) {
    inspected.push(url);
    const verdict = await checkSingleUrl(url, {
      fileRoots,
      resolver,
      maxDataBytes,
    });
    if (!verdict.ok) {
      return { ...verdict, offending_url: truncate(url, 200) };
    }
  }
  return { ok: true, urls: inspected };
}

interface CheckCtx {
  fileRoots: readonly string[];
  resolver: NonNullable<AssetGuardOptions["resolver"]>;
  maxDataBytes: number;
}

async function checkSingleUrl(
  raw: string,
  ctx: CheckCtx,
): Promise<AssetGuardResult> {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return { ok: true, urls: [] };

  // Strip surrounding quotes that survived extraction (rare but possible).
  const cleaned = stripQuotes(trimmed);

  // Reject obvious non-URL artefacts (anchor fragments, javascript: bombs).
  if (cleaned.startsWith("#")) {
    // Same-document anchor -- harmless.
    return { ok: true, urls: [] };
  }
  const lowerScheme = (cleaned.split(":", 1)[0] ?? "").toLowerCase();
  if (lowerScheme === "javascript" || lowerScheme === "vbscript") {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Blocked URL scheme: ${lowerScheme}:`,
      reason: "blocked_scheme",
    };
  }

  if (cleaned.startsWith("data:")) {
    return checkDataUrl(cleaned, ctx.maxDataBytes);
  }

  if (cleaned.startsWith("file:")) {
    return checkFileUrl(cleaned, ctx.fileRoots);
  }

  // Allow protocol-relative `//host/path` to be evaluated as https.
  let urlText = cleaned;
  if (urlText.startsWith("//")) urlText = `https:${urlText}`;

  let parsed: URL;
  try {
    parsed = new URL(urlText);
  } catch {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Invalid URL: ${truncate(cleaned, 120)}`,
      reason: "invalid_url",
    };
  }

  const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
  if (scheme !== "http" && scheme !== "https") {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Blocked URL scheme: ${scheme}:`,
      reason: "blocked_scheme",
    };
  }

  const hostname = parsed.hostname.toLowerCase();
  if (hostname.length === 0) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: "URL is missing a hostname",
      reason: "invalid_url",
    };
  }

  // Hostname text-level checks (literal denylist + .local/.internal suffix).
  if (BLOCKED_HOSTNAMES.has(hostname)) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Blocked hostname: ${hostname}`,
      reason: "blocked_hostname",
    };
  }
  for (const suffix of BLOCKED_HOSTNAME_SUFFIXES) {
    if (hostname === suffix.slice(1) || hostname.endsWith(suffix)) {
      return {
        ok: false,
        code: "asset_fetch_failed",
        message: `Blocked hostname suffix: ${hostname}`,
        reason: "blocked_hostname",
      };
    }
  }

  const isLoopbackHost =
    LOOPBACK_HOSTS.has(hostname) ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "[::1]";

  // Non-HTTPS to non-loopback is rejected (§7.4 + §6.3).
  if (scheme !== "https" && !isLoopbackHost) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Non-HTTPS URL not allowed for non-loopback host: ${hostname}`,
      reason: "non_https",
    };
  }

  // Loopback hostnames + literal loopback IPs are explicitly allowed
  // (compositions can pull from the in-container asset server). They have
  // already passed the scheme check above.
  if (isLoopbackHost) {
    return { ok: true, urls: [] };
  }

  // Literal IP hostnames -- validate directly without DNS.
  if (isIP(hostname) > 0) {
    if (isBlockedIp(hostname)) {
      return {
        ok: false,
        code: "asset_fetch_failed",
        message: `Blocked IP: ${hostname}`,
        reason: "blocked_ip",
      };
    }
    return { ok: true, urls: [] };
  }
  // [::1]-style literal IPv6 wrapped in brackets -- net.isIP needs unwrapping.
  if (hostname.startsWith("[") && hostname.endsWith("]")) {
    const stripped = hostname.slice(1, -1);
    if (isIP(stripped) > 0) {
      if (isBlockedIp(stripped)) {
        return {
          ok: false,
          code: "asset_fetch_failed",
          message: `Blocked IP: ${stripped}`,
          reason: "blocked_ip",
        };
      }
      return { ok: true, urls: [] };
    }
  }

  // Resolve DNS and validate every returned address.
  let addrs: ReadonlyArray<{ address: string; family: number }>;
  try {
    addrs = await ctx.resolver(hostname);
  } catch (err) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `DNS resolution failed for ${hostname}: ${
        err instanceof Error ? err.message : String(err)
      }`,
      reason: "dns_failure",
    };
  }
  if (addrs.length === 0) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `No DNS results for ${hostname}`,
      reason: "dns_failure",
    };
  }
  for (const a of addrs) {
    if (isBlockedIp(a.address)) {
      return {
        ok: false,
        code: "asset_fetch_failed",
        message: `${hostname} resolved to blocked address ${a.address}`,
        reason: "blocked_ip",
      };
    }
  }
  return { ok: true, urls: [] };
}

async function defaultResolver(
  hostname: string,
): Promise<ReadonlyArray<{ address: string; family: number }>> {
  const results = await dnsp.lookup(hostname, { all: true });
  return results;
}

function checkDataUrl(url: string, maxBytes: number): AssetGuardResult {
  // data:[<mediatype>][;base64],<data>
  const commaIdx = url.indexOf(",");
  if (commaIdx < 0) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: "Malformed data: URL (missing comma)",
      reason: "invalid_data_url",
    };
  }
  const meta = url.slice("data:".length, commaIdx).toLowerCase();
  const payload = url.slice(commaIdx + 1);
  let bytes: number;
  if (meta.includes(";base64")) {
    // Approx decoded size: 3/4 of base64 length (ignoring padding for cap).
    bytes = Math.floor((payload.length * 3) / 4);
  } else {
    bytes = Buffer.byteLength(decodeURIComponentSafe(payload), "utf8");
  }
  if (bytes > maxBytes) {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `data: URL too large (${String(bytes)} bytes > ${String(maxBytes)} cap)`,
      reason: "data_too_large",
    };
  }
  return { ok: true, urls: [] };
}

function decodeURIComponentSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

function checkFileUrl(
  url: string,
  fileRoots: readonly string[],
): AssetGuardResult {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `Invalid file: URL: ${truncate(url, 120)}`,
      reason: "invalid_url",
    };
  }
  // file:///path/to/x  -> hostname="" pathname="/path/to/x"
  if (parsed.hostname && parsed.hostname !== "localhost") {
    return {
      ok: false,
      code: "asset_fetch_failed",
      message: `file: URLs must use localhost or empty host (got ${parsed.hostname})`,
      reason: "file_outside_whitelist",
    };
  }
  const filePath = resolvePath(decodeURIComponentSafe(parsed.pathname));
  for (const root of fileRoots) {
    if (filePath === root || filePath.startsWith(`${root}/`)) {
      return { ok: true, urls: [] };
    }
  }
  return {
    ok: false,
    code: "asset_fetch_failed",
    message: `file: path ${filePath} is outside whitelisted asset roots`,
    reason: "file_outside_whitelist",
  };
}

/**
 * Match a literal IPv4 or IPv6 address against the blocked CIDR lists.
 *
 * Hand-rolled to avoid pulling in an `ip` library. v4 is parsed into a
 * 32-bit integer; v6 into 8 16-bit groups. Each CIDR is normalized once on
 * first call (lazy memoization).
 */
function isBlockedIp(addr: string): boolean {
  const family = isIP(addr);
  if (family === 4) {
    const n = ipv4ToInt(addr);
    if (n === null) return true;
    for (const cidr of BLOCKED_V4_CIDRS) {
      if (matchV4(n, cidr)) return true;
    }
    return false;
  }
  if (family === 6) {
    const n = ipv6ToBytes(addr);
    if (n === null) return true;
    for (const cidr of BLOCKED_V6_CIDRS) {
      if (matchV6(n, cidr)) return true;
    }
    // IPv4-mapped IPv6: ::ffff:a.b.c.d -- if the ::ffff:0:0/96 check above
    // didn't catch it (e.g. due to mixed-form parsing), unwrap and re-check.
    const mapped = ipv6MappedToV4(n);
    if (mapped !== null) {
      for (const cidr of BLOCKED_V4_CIDRS) {
        if (matchV4(mapped, cidr)) return true;
      }
    }
    return false;
  }
  return true; // Unparseable = blocked
}

function ipv4ToInt(addr: string): number | null {
  const parts = addr.split(".");
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return null;
    const v = Number.parseInt(p, 10);
    if (v < 0 || v > 255) return null;
    n = (n * 256 + v) >>> 0;
  }
  return n;
}

function matchV4(value: number, cidr: string): boolean {
  const [base, prefixStr] = cidr.split("/");
  if (!base || !prefixStr) return false;
  const prefix = Number.parseInt(prefixStr, 10);
  if (Number.isNaN(prefix) || prefix < 0 || prefix > 32) return false;
  const baseInt = ipv4ToInt(base);
  if (baseInt === null) return false;
  if (prefix === 0) return true;
  // Use BigInt to safely shift; >>> 32 is undefined behavior in JS.
  const mask = ((0xffffffff << (32 - prefix)) >>> 0) >>> 0;
  return (value & mask) === (baseInt & mask);
}

function ipv6ToBytes(addr: string): Uint8Array | null {
  // Strip zone id (fe80::1%eth0).
  const noZone = addr.split("%", 1)[0] ?? addr;
  let s = noZone;

  // Embedded IPv4 (::ffff:1.2.3.4) -- expand to ::ffff:0102:0304.
  const v4Match = /^(.*?:)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/.exec(s);
  if (v4Match) {
    const head = v4Match[1] ?? "";
    const v4 = v4Match[2] ?? "";
    const v4Int = ipv4ToInt(v4);
    if (v4Int === null) return null;
    const high = (v4Int >>> 16) & 0xffff;
    const low = v4Int & 0xffff;
    s = `${head}${high.toString(16)}:${low.toString(16)}`;
  }

  const doubleColonIdx = s.indexOf("::");
  let groups: string[];
  if (doubleColonIdx >= 0) {
    const left = s.slice(0, doubleColonIdx).split(":").filter(Boolean);
    const right = s.slice(doubleColonIdx + 2).split(":").filter(Boolean);
    const fillCount = 8 - left.length - right.length;
    if (fillCount < 0) return null;
    groups = [...left, ...Array<string>(fillCount).fill("0"), ...right];
  } else {
    groups = s.split(":");
  }
  if (groups.length !== 8) return null;
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 8; i++) {
    const g = groups[i];
    if (!g || !/^[0-9a-fA-F]{1,4}$/.test(g)) return null;
    const v = Number.parseInt(g, 16);
    if (Number.isNaN(v) || v < 0 || v > 0xffff) return null;
    bytes[i * 2] = (v >>> 8) & 0xff;
    bytes[i * 2 + 1] = v & 0xff;
  }
  return bytes;
}

function matchV6(value: Uint8Array, cidr: string): boolean {
  const [base, prefixStr] = cidr.split("/");
  if (!base || !prefixStr) return false;
  const prefix = Number.parseInt(prefixStr, 10);
  if (Number.isNaN(prefix) || prefix < 0 || prefix > 128) return false;
  const baseBytes = ipv6ToBytes(base);
  if (baseBytes === null) return false;

  const fullBytes = Math.floor(prefix / 8);
  const remainBits = prefix % 8;
  for (let i = 0; i < fullBytes; i++) {
    if (value[i] !== baseBytes[i]) return false;
  }
  if (remainBits === 0) return true;
  const mask = (0xff << (8 - remainBits)) & 0xff;
  const v = value[fullBytes] ?? 0;
  const b = baseBytes[fullBytes] ?? 0;
  return (v & mask) === (b & mask);
}

function ipv6MappedToV4(bytes: Uint8Array): number | null {
  // ::ffff:0:0/96 -> first 10 bytes 0, bytes 10-11 = 0xff 0xff
  for (let i = 0; i < 10; i++) {
    if (bytes[i] !== 0) return null;
  }
  if (bytes[10] !== 0xff || bytes[11] !== 0xff) return null;
  return (
    ((bytes[12] ?? 0) << 24) |
    ((bytes[13] ?? 0) << 16) |
    ((bytes[14] ?? 0) << 8) |
    (bytes[15] ?? 0)
  ) >>> 0;
}

/**
 * Extract every URL referenced by the composition HTML. Deduped, in scan
 * order. Inputs that look like fragment refs (#id) or empty strings are
 * filtered.
 */
export function extractAssetUrls(html: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const push = (raw: string | undefined): void => {
    if (!raw) return;
    const v = stripQuotes(raw.trim());
    if (v.length === 0) return;
    if (v.startsWith("#")) return;
    if (seen.has(v)) return;
    seen.add(v);
    out.push(v);
  };

  // src= and href= attributes (single or double quoted).
  const ATTR_RE = /\b(?:src|href)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))/gi;
  for (const m of html.matchAll(ATTR_RE)) {
    push(m[2] ?? m[3] ?? m[4]);
  }

  // url(...) inside style="..." or <style>...</style>.
  const URL_FN_RE = /url\(\s*("([^"]*)"|'([^']*)'|([^)]*))\s*\)/gi;
  for (const m of html.matchAll(URL_FN_RE)) {
    push(m[2] ?? m[3] ?? m[4]);
  }

  // <link href=...> already covered by ATTR_RE above. <meta http-equiv
  // refresh URL=...> is left out -- not a render asset.
  return out;
}

function stripQuotes(s: string): string {
  if (s.length >= 2) {
    const first = s[0];
    const last = s[s.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return s.slice(1, -1);
    }
  }
  return s;
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 3)}...`;
}
