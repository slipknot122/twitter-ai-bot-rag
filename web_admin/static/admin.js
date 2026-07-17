(() => {
  const sidebar = document.querySelector('#sidebar');
  const scrim = document.querySelector('[data-menu-close]');
  const toggle = document.querySelector('[data-menu-toggle]');
  document.querySelectorAll('.sidebar .nav-item').forEach((link) => {
    const target = new URL(link.href, window.location.origin);
    const exact = target.pathname === window.location.pathname;
    link.classList.toggle('active', exact && (target.pathname !== '/' || window.location.pathname === '/'));
    if (exact) link.setAttribute('aria-current', 'page');
  });
  const closeMenu = () => {
    sidebar?.classList.remove('open');
    if (scrim) scrim.hidden = true;
    toggle?.setAttribute('aria-expanded', 'false');
  };
  toggle?.addEventListener('click', () => {
    const open = !sidebar?.classList.contains('open');
    sidebar?.classList.toggle('open', open);
    if (scrim) scrim.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  });
  scrim?.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => event.key === 'Escape' && closeMenu());

  window.adminToast = (message, type = 'success') => {
    const region = document.querySelector('#toast-region');
    if (!region) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type === 'error' ? 'error' : ''}`;
    toast.textContent = message;
    region.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  };

  window.adminConfirm = ({ title, message, action = 'Підтвердити', danger = true }) => {
    const dialog = document.querySelector('#confirm-dialog');
    if (!dialog) return Promise.resolve(false);
    dialog.querySelector('#confirm-title').textContent = title;
    dialog.querySelector('#confirm-message').textContent = message;
    const button = dialog.querySelector('#confirm-action');
    button.textContent = action;
    button.className = `button ${danger ? 'danger' : ''}`;
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    });
  };
})();
