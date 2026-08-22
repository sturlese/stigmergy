// The theme, stamped on <html> BEFORE the first paint.
//
// A classic script in <head> rather than part of the module graph, and that is the whole reason
// this file exists: a module is deferred until after the document is parsed, so an operator who
// chose Dark would get a flash of the light console on every load. The console ships under
// `script-src 'self'`, which refuses an inline script — so the early work has to be a file.
//
// Three states: no attribute (Auto — `color-scheme: light dark` follows the OS), `light`, `dark`.
// `ui.js` reads and writes the same key through its own helpers; a classic script cannot be
// imported by a module, so the KEY and the state names are spelled in both places, and
// `tests/admin/test_static_discipline.py` pins the two spellings against each other.
(function stampTheme() {
  var KEY = "stigmergy-ops-theme";
  try {
    var chosen = localStorage.getItem(KEY);
    if (chosen === "light" || chosen === "dark") {
      document.documentElement.setAttribute("data-theme", chosen);
    }
  } catch (error) {
    // A browser with storage denied (private mode, a locked-down profile) simply follows the OS.
    // Never let a preference stop the console from rendering.
  }
})();
