if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
  document.documentElement.classList.add("dark");
}
// Tabs and the step-by-step brief collapse only when scripts can open them again.
document.documentElement.classList.add("js");
