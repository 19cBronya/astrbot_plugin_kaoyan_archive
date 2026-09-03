import assert from "node:assert/strict";
import test from "node:test";

import {
  MATH_DELIMITERS,
  parseSummaryBlocks,
  readDisplayMath,
} from "../pages/archive/math-renderer.mjs";

test("combines multiline display math before Markdown paragraphs are built", () => {
  const blocks = parseSummaryBlocks(String.raw`## 题目

检查手写笔记，并整理方程

\[
F\left(x+\frac{z}{y}, y+\frac{z}{x}\right)=0
\]

求 \(z_x\) 与 $z_y$。`);

  assert.deepEqual(blocks.map((block) => block.type), [
    "heading",
    "paragraph",
    "math",
    "paragraph",
  ]);
  assert.match(blocks[2].expression, /\\frac\{z\}\{y\}/);
});

test("supports dollar blocks and common display environments", () => {
  const dollar = readDisplayMath(["$$", "x^2+y^2=1", "$$"], 0);
  assert.equal(dollar.expression, "x^2+y^2=1");
  assert.equal(dollar.nextIndex, 3);

  const aligned = readDisplayMath([
    String.raw`\begin{align}`,
    String.raw`x &= y + 1 \\`,
    String.raw`z &= 2`,
    String.raw`\end{align}`,
  ], 0);
  assert.match(aligned.expression, /begin\{align\}/);
  assert.equal(aligned.nextIndex, 4);
});

test("auto-render configuration includes display and inline delimiters", () => {
  assert.deepEqual(
    MATH_DELIMITERS.filter(({ left }) => ["\\[", "\\(", "$$", "$"].includes(left))
      .map(({ left, display }) => [left, display]),
    [["$$", true], ["\\[", true], ["\\(", false], ["$", false]],
  );
});
