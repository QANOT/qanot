/**
 * Asset URL allowlist tests.
 *
 * Covers ARCHITECTURE §6.3 + §7.4: every URL in the composition is checked
 * before any Chromium subprocess is spawned. Reject private IPs, IPv6 ULA,
 * IPv4-mapped IPv6, NAT64, *.local/.internal, non-HTTPS to non-loopback,
 * file:// outside the whitelist, oversize data: URLs.
 *
 * Strategy: inject a deterministic DNS resolver so the tests don't actually
 * hit the network -- the production resolver is exercised in integration.
 */

import { describe, expect, it } from "vitest";
import {
  checkCompositionAssets,
  extractAssetUrls,
  MAX_DATA_URL_BYTES,
} from "../../src/render/asset_guard.js";

const FILE_ROOTS = ["/app/compositions", "/app/assets"] as const;

/**
 * Build a stub resolver that returns the supplied addresses for every
 * hostname. Family is inferred from the literal (4 vs 6).
 */
function fixedResolver(addrs: ReadonlyArray<string>) {
  return async () =>
    addrs.map((a) => ({ address: a, family: a.includes(":") ? 6 : 4 }));
}

function htmlWith(...urls: string[]): string {
  // Mix attribute styles so extraction is exercised, not just the URL check.
  const tags = urls.map((u, i) =>
    i % 2 === 0
      ? `<img src="${u}">`
      : `<div style="background-image: url('${u}')"></div>`,
  );
  return `<!doctype html><html><body>${tags.join("\n")}</body></html>`;
}

describe("checkCompositionAssets — schemes", () => {
  it("accepts https URLs that resolve to public IPs", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://cdn.example.com/font.woff2"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver(["8.8.8.8"]) },
    );
    expect(r.ok).toBe(true);
  });

  it("accepts http://localhost", async () => {
    const r = await checkCompositionAssets(
      htmlWith("http://localhost:8080/asset.svg"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(true);
  });

  it("rejects http:// to a public host (non-HTTPS)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("http://example.com/x.png"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver(["1.2.3.4"]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("non_https");
      expect(r.code).toBe("asset_fetch_failed");
    }
  });

  it("rejects javascript: scheme", async () => {
    const r = await checkCompositionAssets(
      `<a href="javascript:alert(1)">x</a>`,
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_scheme");
  });
});

describe("checkCompositionAssets — IPs", () => {
  it("rejects literal RFC1918 IPv4 (10.0.0.0/8)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://10.0.0.5/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects literal 192.168.x", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://192.168.1.1/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects 169.254.169.254 (cloud metadata)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://169.254.169.254/latest/meta-data/"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects 100.64.0.0/10 (CGNAT)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://100.64.10.20/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects DNS resolutions to private IPs", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://attacker.example/x"),
      {
        fileAssetRoots: FILE_ROOTS,
        resolver: fixedResolver(["10.0.0.42"]),
      },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects IPv6 ULA fc00::/7", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://attacker.example/x"),
      {
        fileAssetRoots: FILE_ROOTS,
        resolver: fixedResolver(["fc00::1"]),
      },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects IPv4-mapped IPv6 (::ffff:127.0.0.1)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://attacker.example/x"),
      {
        fileAssetRoots: FILE_ROOTS,
        resolver: fixedResolver(["::ffff:127.0.0.1"]),
      },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });

  it("rejects link-local IPv6 (fe80::/10)", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://attacker.example/x"),
      {
        fileAssetRoots: FILE_ROOTS,
        resolver: fixedResolver(["fe80::1"]),
      },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_ip");
  });
});

describe("checkCompositionAssets — hostnames", () => {
  it("rejects metadata.google.internal", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://metadata.google.internal/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver(["8.8.8.8"]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_hostname");
  });

  it("rejects *.internal hostnames", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://api.svc.internal/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver(["8.8.8.8"]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_hostname");
  });

  it("rejects *.local hostnames", async () => {
    const r = await checkCompositionAssets(
      htmlWith("https://printer.local/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver(["8.8.8.8"]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("blocked_hostname");
  });
});

describe("checkCompositionAssets — data: URLs", () => {
  it("accepts data: under the 1 MB cap", async () => {
    const small = `data:image/png;base64,${"A".repeat(1024)}`;
    const r = await checkCompositionAssets(htmlWith(small), {
      fileAssetRoots: FILE_ROOTS,
      resolver: fixedResolver([]),
    });
    expect(r.ok).toBe(true);
  });

  it("rejects data: over 1 MB", async () => {
    // base64 char count of ceil(MAX*4/3) bytes -> definitely over cap.
    const big = `data:image/png;base64,${"A".repeat(MAX_DATA_URL_BYTES * 2)}`;
    const r = await checkCompositionAssets(htmlWith(big), {
      fileAssetRoots: FILE_ROOTS,
      resolver: fixedResolver([]),
    });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("data_too_large");
  });

  it("rejects malformed data: URLs", async () => {
    const r = await checkCompositionAssets(
      htmlWith("data:no-comma-here"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("invalid_data_url");
  });
});

describe("checkCompositionAssets — file: URLs", () => {
  it("accepts file:// inside whitelisted roots", async () => {
    const r = await checkCompositionAssets(
      htmlWith("file:///app/assets/font.woff2"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(true);
  });

  it("rejects file:// outside whitelisted roots", async () => {
    const r = await checkCompositionAssets(
      htmlWith("file:///etc/passwd"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("file_outside_whitelist");
  });

  it("rejects file:// with a non-localhost host", async () => {
    const r = await checkCompositionAssets(
      htmlWith("file://server/share/x"),
      { fileAssetRoots: FILE_ROOTS, resolver: fixedResolver([]) },
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("file_outside_whitelist");
  });
});

describe("extractAssetUrls", () => {
  it("collects src=, href=, and url(...) references and dedupes", () => {
    const html = `<!doctype html><html><head>
      <link href="https://fonts.example/Inter.css">
      <style>body { background-image: url('https://cdn.example/bg.png'); }</style>
    </head><body>
      <img src='https://cdn.example/a.png'>
      <img src="https://cdn.example/a.png">  <!-- duplicate -->
      <video src=https://cdn.example/v.mp4></video>
      <a href="https://other.example/x">x</a>
      <div style="background: url(https://cdn.example/inline.png) center"></div>
    </body></html>`;
    const urls = extractAssetUrls(html);
    expect(urls).toEqual([
      "https://fonts.example/Inter.css",
      "https://cdn.example/a.png",
      "https://cdn.example/v.mp4",
      "https://other.example/x",
      "https://cdn.example/bg.png",
      "https://cdn.example/inline.png",
    ]);
  });

  it("ignores anchor fragments (#section)", () => {
    const html = `<a href="#main">jump</a><img src="">`;
    expect(extractAssetUrls(html)).toEqual([]);
  });
});
