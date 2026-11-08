import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [runtimeRoot, inputPath, outputPath] = process.argv.slice(2);
if (!runtimeRoot || !inputPath || !outputPath) {
  throw new Error("usage: penrose_render.mjs RUNTIME_ROOT INPUT.json OUTPUT.svg");
}
const moduleUrl = (relative) => pathToFileURL(path.join(runtimeRoot, relative)).href;
const { JSDOM } = await import(
  moduleUrl(path.join("node_modules", "jsdom", "lib", "api.js"))
);
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.Node = dom.window.Node;
globalThis.Element = dom.window.Element;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.SVGElement = dom.window.SVGElement;
globalThis.SVGSVGElement = dom.window.SVGSVGElement;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.XMLSerializer = dom.window.XMLSerializer;

const core = await import(
  moduleUrl(path.join("node_modules", "@penrose", "core", "dist", "index.js"))
);
const program = JSON.parse(await fs.readFile(inputPath, "utf8"));
const compiled = await core.compile({
  domain: String(program.domain ?? ""),
  substance: String(program.substance ?? ""),
  style: String(program.style ?? ""),
  variation: String(program.variation ?? "MathProblemBank"),
  excludeWarnings: Array.isArray(program.excludeWarnings)
    ? program.excludeWarnings.map(String)
    : [],
});
if (compiled.isErr()) {
  throw new Error(core.showError(compiled.error));
}
const optimized = core.optimize(compiled.value);
if (optimized.isErr()) {
  throw new Error(core.showError(optimized.error));
}
const svgNode = await core.toSVG(
  optimized.value,
  async () => undefined,
  "mpb-diagram",
);
await fs.writeFile(outputPath, svgNode.outerHTML, "utf8");
