"use strict";

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const code = document.getElementById(button.dataset.copy);
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(code.textContent.trim() + "\n");
      button.textContent = "Copied ✓";
      status.textContent = "Commands copied to clipboard.";
      window.setTimeout(() => {
        button.innerHTML = 'Copy <span aria-hidden="true">⧉</span>';
      }, 2000);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent =
        "Commands selected. Press your system copy shortcut to copy.";
      code.parentElement.focus();
    }
  });
}
