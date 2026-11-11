/* Apply the stored colour scheme before the first paint (auto = follow the OS). */
(function () {
  var theme = "auto";
  try {
    var stored = localStorage.getItem("albert-admin-theme");
    if (stored === "light" || stored === "dark" || stored === "auto") theme = stored;
  } catch (e) { /* storage blocked: follow the OS */ }
  document.documentElement.setAttribute("data-theme", theme);
})();
