export function pathDiffDisclosure(h, pageRole, contents) {
  const details = h("details", { class: "path-detail" }, h("summary", {},
    pageRole === "source" ? "Show archived source diff" : "Show line changes"), contents);
  details.open = pageRole !== "source";
  return details;
}

export function exactPatchDisclosure(h, patch) {
  return h("details", {}, h("summary", {}, "Exact Git patch"),
    h("pre", { class: "exact" }, patch));
}
