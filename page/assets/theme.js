// 在绘制页面前恢复主题；三个页面共用同一个偏好，不发送到服务端。
(() => {
  let theme = 'dark';
  try { theme = localStorage.getItem('education-theme') === 'light' ? 'light' : 'dark'; } catch {}
  document.documentElement.dataset.theme = theme;

  function updateButtons() {
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      const light = document.documentElement.dataset.theme === 'light';
      button.textContent = light ? '☀ 亮色' : '◐ 暗色';
      button.setAttribute('aria-label', light ? '切换暗色模式' : '切换亮色模式');
      button.title = light ? '当前亮色，点击切换暗色' : '当前暗色，点击切换亮色';
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    updateButtons();
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.addEventListener('click', () => {
        theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
        document.documentElement.dataset.theme = theme;
        try { localStorage.setItem('education-theme', theme); } catch {}
        updateButtons();
      });
    });
  });
  window.addEventListener('storage', event => {
    if (event.key === 'education-theme') {
      document.documentElement.dataset.theme = event.newValue === 'light' ? 'light' : 'dark';
      updateButtons();
    }
  });
})();
