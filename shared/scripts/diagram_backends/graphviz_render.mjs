import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [runtimeRoot, inputPath, outputPath] = process.argv.slice(2);
if (!runtimeRoot || !inputPath || !outputPath) {
  throw new Error("usage: graphviz_render.mjs RUNTIME_ROOT INPUT.dot OUTPUT.svg");
}
const moduleUrl = pathToFileURL(
  path.join(runtimeRoot, "node_modules", "@viz-js", "viz", "dist", "viz.js"),
).href;
const { instance } = await import(moduleUrl);
const viz = await instance();
const source = await fs.readFile(inputPath, "utf8");
const svg = viz.renderString(source, { format: "svg", engine: "dot" });
await fs.writeFile(outputPath, svg, "utf8");
