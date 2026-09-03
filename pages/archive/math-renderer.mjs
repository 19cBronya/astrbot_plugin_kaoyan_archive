const BLOCK_ENVIRONMENTS = new Set([
  "equation",
  "equation*",
  "align",
  "align*",
  "alignat",
  "alignat*",
  "gather",
  "gather*",
  "CD",
]);

export const MATH_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\begin{equation}", right: "\\end{equation}", display: true },
  { left: "\\begin{equation*}", right: "\\end{equation*}", display: true },
  { left: "\\begin{align}", right: "\\end{align}", display: true },
  { left: "\\begin{align*}", right: "\\end{align*}", display: true },
  { left: "\\begin{alignat}", right: "\\end{alignat}", display: true },
  { left: "\\begin{alignat*}", right: "\\end{alignat*}", display: true },
  { left: "\\begin{gather}", right: "\\end{gather}", display: true },
  { left: "\\begin{gather*}", right: "\\end{gather*}", display: true },
  { left: "\\begin{CD}", right: "\\end{CD}", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "$", right: "$", display: false },
];

function delimitedBlock(lines, startIndex, left, right) {
  const first = lines[startIndex].trim();
  if (!first.startsWith(left)) return null;

  const firstRemainder = first.slice(left.length);
  const sameLineEnd = firstRemainder.indexOf(right);
  if (sameLineEnd >= 0) {
    if (firstRemainder.slice(sameLineEnd + right.length).trim()) return null;
    return {
      expression: firstRemainder.slice(0, sameLineEnd).trim(),
      source: lines[startIndex],
      nextIndex: startIndex + 1,
    };
  }

  const expressionLines = firstRemainder ? [firstRemainder] : [];
  for (let index = startIndex + 1; index < lines.length; index += 1) {
    const endAt = lines[index].indexOf(right);
    if (endAt < 0) {
      expressionLines.push(lines[index]);
      continue;
    }
    if (lines[index].slice(endAt + right.length).trim()) return null;
    expressionLines.push(lines[index].slice(0, endAt));
    return {
      expression: expressionLines.join("\n").trim(),
      source: lines.slice(startIndex, index + 1).join("\n"),
      nextIndex: index + 1,
    };
  }
  return null;
}

function environmentBlock(lines, startIndex) {
  const first = lines[startIndex].trim();
  const match = first.match(/^\\begin\{([^}]+)\}/);
  if (!match || !BLOCK_ENVIRONMENTS.has(match[1])) return null;
  const endToken = `\\end{${match[1]}}`;
  for (let index = startIndex; index < lines.length; index += 1) {
    const endAt = lines[index].indexOf(endToken);
    if (endAt < 0) continue;
    if (lines[index].slice(endAt + endToken.length).trim()) return null;
    const expression = lines.slice(startIndex, index + 1).join("\n").trim();
    return { expression, source: expression, nextIndex: index + 1 };
  }
  return null;
}

export function readDisplayMath(lines, startIndex) {
  return (
    delimitedBlock(lines, startIndex, "\\[", "\\]")
    || delimitedBlock(lines, startIndex, "$$", "$$")
    || environmentBlock(lines, startIndex)
  );
}

export function parseSummaryBlocks(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const blocks = [];
  for (let index = 0; index < lines.length;) {
    const rawLine = lines[index];
    const line = rawLine.trim();
    if (!line) {
      index += 1;
      continue;
    }

    const math = readDisplayMath(lines, index);
    if (math) {
      blocks.push({ type: "math", ...math });
      index = math.nextIndex;
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      index += 1;
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].trim().match(/^[-*]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push({ type: "list", items });
      continue;
    }

    blocks.push({ type: "paragraph", text: line });
    index += 1;
  }
  return blocks;
}

const KATEX_OPTIONS = {
  throwOnError: false,
  strict: "ignore",
  trust: false,
  output: "htmlAndMathml",
  errorColor: "#b5413e",
};

const LOOSE_MATH_SYMBOLS = new Set([
  "∫", "∑", "√", "∞", "≤", "≥", "≠", "≈", "±", "×", "÷", "·", "²", "³", "′", "″",
]);

function isLooseMathCharacter(character) {
  return /[A-Za-z0-9\s_=+\-*/^()[\]{}|.,\\]/u.test(character)
    || LOOSE_MATH_SYMBOLS.has(character);
}

function looksLikeLooseMath(expression) {
  return /[A-Za-z0-9]/u.test(expression)
    && (/[=_^]/u.test(expression) || /[∫∑√∞≤≥≠≈±×÷²³]/u.test(expression));
}

function normalizeLooseMath(expression) {
  return expression
    .replaceAll("∫", "\\int ")
    .replaceAll("∑", "\\sum ")
    .replaceAll("∞", "\\infty ")
    .replaceAll("≤", "\\le ")
    .replaceAll("≥", "\\ge ")
    .replaceAll("≠", "\\ne ")
    .replaceAll("≈", "\\approx ")
    .replaceAll("±", "\\pm ")
    .replaceAll("×", "\\times ")
    .replaceAll("÷", "\\div ")
    .replaceAll("²", "^2")
    .replaceAll("³", "^3")
    .replace(/_([A-Za-z]{2,})/gu, "_{$1}");
}

function explicitMathAt(source, index) {
  const delimiters = [
    ["\\[", "\\]"],
    ["\\(", "\\)"],
    ["$$", "$$"],
    ["$", "$"],
  ];
  for (const [left, right] of delimiters) {
    if (!source.startsWith(left, index)) continue;
    const end = source.indexOf(right, index + left.length);
    if (end >= 0) return source.slice(index, end + right.length);
  }
  return "";
}

export function inferInlineMath(text) {
  const source = String(text || "");
  let rendered = "";
  for (let index = 0; index < source.length;) {
    if (/\s/u.test(source[index])) {
      let next = index;
      while (next < source.length && /\s/u.test(source[next])) next += 1;
      const spacedExplicit = explicitMathAt(source, next);
      if (spacedExplicit) {
        rendered += source.slice(index, next) + spacedExplicit;
        index = next + spacedExplicit.length;
        continue;
      }
    }
    const explicit = explicitMathAt(source, index);
    if (explicit) {
      rendered += explicit;
      index += explicit.length;
      continue;
    }
    if (!isLooseMathCharacter(source[index])) {
      rendered += source[index];
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < source.length && isLooseMathCharacter(source[end])) end += 1;
    const candidate = source.slice(index, end);
    const leading = candidate.match(/^\s*/u)?.[0] || "";
    const trailing = candidate.match(/\s*$/u)?.[0] || "";
    const expression = candidate.slice(leading.length, candidate.length - trailing.length);
    rendered += looksLikeLooseMath(expression)
      ? `${leading}\\(${normalizeLooseMath(expression)}\\)${trailing}`
      : candidate;
    index = end;
  }
  return rendered;
}

export function renderDisplayMath(target, expression, source) {
  target.classList.add("math-block");
  if (typeof globalThis.katex?.render !== "function") {
    target.classList.add("math-fallback");
    target.textContent = source || expression;
    return false;
  }
  try {
    globalThis.katex.render(expression, target, {
      ...KATEX_OPTIONS,
      displayMode: true,
    });
    return true;
  } catch (error) {
    target.classList.add("math-fallback");
    target.textContent = source || expression;
    console.warn("公式渲染失败，已保留原文", error);
    return false;
  }
}

export function renderMath(container) {
  if (!container || typeof globalThis.renderMathInElement !== "function") return false;
  try {
    globalThis.renderMathInElement(container, {
      ...KATEX_OPTIONS,
      delimiters: MATH_DELIMITERS,
      ignoredClasses: ["katex", "math-block", "math-fallback"],
      errorCallback(message, error) {
        console.warn("公式自动渲染失败，已保留原文", message, error);
      },
    });
    return true;
  } catch (error) {
    console.warn("公式自动渲染不可用，已保留原文", error);
    return false;
  }
}
