"use strict";

const messages = document.documentElement.lang.startsWith("zh")
  ? {
      copied: "已复制 ✓",
      success: "命令已复制到剪贴板。",
      selected: "命令已选中，请使用系统复制快捷键进行复制。",
    }
  : {
      copied: "Copied ✓",
      success: "Commands copied to clipboard.",
      selected: "Commands selected. Press your system copy shortcut to copy.",
    };

for (const button of document.querySelectorAll("[data-copy]")) {
  const initialLabel = button.innerHTML;
  let resetTimer;
  button.addEventListener("click", async () => {
    const code = document.getElementById(button.dataset.copy);
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(code.textContent.trim() + "\n");
      button.textContent = messages.copied;
      status.textContent = messages.success;
      window.clearTimeout(resetTimer);
      resetTimer = window.setTimeout(() => {
        button.innerHTML = initialLabel;
      }, 2000);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = messages.selected;
      code.parentElement.focus();
    }
  });
}
