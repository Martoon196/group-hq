// Reused unchanged from Martin's Claude whatsapp-autodm/src/core.js matcher.
const normalise = (s) =>
  String(s || "").toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, " ").replace(/\s+/g, " ").trim();

function findTrigger(body, triggers, matchMode = "exact") {
  const text = normalise(body);
  if (!text) return null;
  for (const t of triggers) {
    for (const kw of t.keywords) {
      const k = normalise(kw);
      const hit = matchMode === "word" ? ` ${text} `.includes(` ${k} `) : text === k;
      if (hit) return { trigger: t, keyword: kw };
    }
  }
  return null;
}


module.exports = { normalise, findTrigger };
