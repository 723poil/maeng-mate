import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { dirname, extname, resolve } from "node:path";

const [source, destination] = process.argv.slice(2);
if (!source || !destination) throw new Error("사용법: node build.mjs <공개 사이트 폴더> <Worker 출력 파일>");

const types = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
  ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
  ".woff2": "font/woff2", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8",
};
const assets = {};

/** 공개 페이지에서 사용하는 파일만 Worker에 포함하며 심볼릭 링크는 허용하지 않는다. */
async function collect(directory = "") {
  for (const entry of await readdir(resolve(source, directory), { withFileTypes: true })) {
    const name = directory ? directory + "/" + entry.name : entry.name;
    if (entry.name.startsWith(".")) continue;
    if (entry.isDirectory()) {
      if (name === "assets" || name.startsWith("assets/")) await collect(name);
      continue;
    }
    const type = types[extname(name)];
    if (!type || !entry.isFile()) throw new Error("지원하지 않는 사이트 파일: " + name);
    assets["/" + name] = { type, data: (await readFile(resolve(source, name))).toString("base64") };
  }
}
await collect();
if (!assets["/index.html"] || !assets["/download.js"]) throw new Error("필수 사이트 파일이 없습니다.");
const handler = await readFile(new URL("./worker.mjs", import.meta.url), "utf8");
await mkdir(dirname(destination), { recursive: true });
await writeFile(destination, "const siteAssets = " + JSON.stringify(assets) + ";\n" + handler);
