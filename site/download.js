const releases = "https://github.com/723poil/maeng-mate/releases";
const sparkleNamespace = "http://www.andymatuschak.org/xml-namespaces/sparkle";

/** 공개 Sparkle 피드의 버전과 DMG 주소가 일치할 때만 모든 버튼을 함께 바꾼다. */
async function refreshDownloadLinks() {
  try {
    const response = await fetch("/latest-appcast.xml", { credentials: "omit" });
    if (!response.ok) return;
    const feed = new DOMParser().parseFromString(await response.text(), "application/xml");
    const item = feed.querySelector("rss > channel > item");
    const version = item?.getElementsByTagNameNS(sparkleNamespace, "version")[0]?.textContent?.trim();
    if (!version || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) return;

    const tag = "v" + version;
    const url = `${releases}/download/${tag}/Catch-${version}-macOS-arm64.dmg`;
    if (item.querySelector("enclosure")?.getAttribute("url") !== url) return;
    for (const link of document.querySelectorAll("[data-release-download]")) link.href = url;
    for (const label of document.querySelectorAll("[data-release-label]")) label.textContent = tag + " DMG 다운로드";
    for (const link of document.querySelectorAll("[data-release-notes]")) link.href = releases + "/tag/" + tag;
  } catch {
    // 피드 조회가 실패하면 HTML의 최신 릴리스 링크를 사용한다.
  }
}

refreshDownloadLinks();
