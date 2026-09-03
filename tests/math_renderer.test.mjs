import assert from "node:assert/strict";
import test from "node:test";

import {
  inferInlineMath,
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

test("infers obvious undelimited formulas in archived overviews", () => {
  const inferred = inferInlineMath(
    "计算 z=∫_0^1 |xy-t|f(t)dt，并由 z_xx=2y²f(xy) 得 z_yy=2x²f(xy)。",
  );

  assert.match(inferred, /\\\(z=\\int _0\^1 \|xy-t\|f\(t\)dt\\\)/);
  assert.match(inferred, /\\\(z_\{xx\}=2y\^2f\(xy\)\\\)/);
  assert.match(inferred, /\\\(z_\{yy\}=2x\^2f\(xy\)\\\)/);
});

test("preserves explicit formulas and ordinary prose", () => {
  assert.equal(
    inferInlineMath(String.raw`已有 \(z_x=1\)，编号 2026。`),
    String.raw`已有 \(z_x=1\)，编号 2026。`,
  );
});
