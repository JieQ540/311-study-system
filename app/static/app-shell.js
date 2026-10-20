/* 页面只声明 data-page；导航和表格的窄屏容器由此维护，不参与业务操作。 */
(function () {
  'use strict';
  const pages = [
    ['exam','/','模拟考试'], ['daily','/daily','日常练习'], ['practice','/practice','主观题专项'],
    ['coverage','/coverage','全科覆盖'], ['logs','/logs','练习日志'], ['reports','/reports','学习复盘'], ['settings','/settings','AI 设置']
  ];
  const host = document.querySelector('[data-app-nav]');
  if (host) {
    const brand = document.createElement('a');
    brand.className = 'app-brand'; brand.href = '/';
    brand.innerHTML = '<strong>311</strong><span>备考系统</span>';
    const links = document.createElement('div'); links.className = 'app-links';
    pages.forEach(([page, href, label]) => {
      const link = document.createElement('a');
      link.href = href; link.textContent = label; link.dataset.navLink = page;
      if (document.body.dataset.page === page) link.setAttribute('aria-current', 'page');
      links.appendChild(link);
    });
    host.replaceChildren(brand, links);
  }
  const main = document.querySelector('main');
  if (!main) return;
  function fitTables() {
    main.querySelectorAll('table').forEach(table => {
      if (table.parentElement.classList.contains('table-scroll')) return;
      const scroll = document.createElement('div');
      scroll.className = 'table-scroll'; scroll.tabIndex = 0;
      scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', '表格，可横向滚动查看');
      table.before(scroll); scroll.appendChild(table);
    });
  }
  fitTables();
  new MutationObserver(fitTables).observe(main, {childList:true, subtree:true});
})();
