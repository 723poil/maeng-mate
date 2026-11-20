const publicFeed = "https://github.com/723poil/maeng-mate/releases/latest/download/appcast.xml";

/** 공개 릴리스 피드는 같은 출처로 전달해 브라우저의 교차 출처 제한을 피한다. */
async function releaseFeed(request) {
  try {
    const upstream = await fetch(publicFeed, {
      signal: AbortSignal.timeout(5000),
      cf: { cacheEverything: true, cacheTtl: 60 },
    });
    if (upstream.ok) {
      return new Response(request.method === "HEAD" ? null : upstream.body, {
        headers: { "Content-Type": "application/xml; charset=utf-8", "Cache-Control": "public, max-age=60" },
      });
    }
  } catch {
    // GitHub 지연이나 연결 실패 시 사이트 본문과 최신 릴리스 링크는 계속 제공한다.
  }
  return new Response(null, { status: 503, headers: { "Cache-Control": "no-store" } });
}

export default {
  /** 빌드에 포함한 공개 파일과 고정된 업데이트 피드 한 곳만 읽는다. */
  async fetch(request) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    const pathname = new URL(request.url).pathname;
    if (pathname === "/latest-appcast.xml") return releaseFeed(request);

    const key = pathname === "/" ? "/index.html" : pathname;
    if (!Object.hasOwn(siteAssets, key)) return new Response(null, { status: 404 });
    const asset = siteAssets[key];
    if (request.method === "GET" && !asset.bytes) {
      const raw = atob(asset.data);
      asset.bytes = new Uint8Array(raw.length);
      for (let index = 0; index < raw.length; index++) asset.bytes[index] = raw.charCodeAt(index);
    }
    return new Response(request.method === "HEAD" ? null : asset.bytes, {
      headers: { "Content-Type": asset.type, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache" },
    });
  },
};
