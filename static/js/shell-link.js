/* 外壳内切换（shell.html 配套）。
 * 四个界面在同一外壳里以 iframe 共存：页面内的跳转入口（原 target="_blank"）
 * 在外壳里点 = 通知外壳切 tab；独立打开时（无外壳）不拦截，
 * 交给 href 落到 /admin、/orchestration 等外壳路由，行为不变。 */
function wbSwitch(ev, view) {
    if (window.parent !== window) {
        ev.preventDefault();
        window.parent.postMessage({ wbSwitch: view }, "*");
    }
}
