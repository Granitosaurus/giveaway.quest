// Loaded synchronously in <head>: apply a saved theme choice before first paint.
// With nothing saved the stylesheet's own prefers-color-scheme rule picks quest / questdark.
try {
  var t = localStorage.getItem("theme");
  if (t === "quest" || t === "questdark") document.documentElement.dataset.theme = t;
} catch (e) {}
