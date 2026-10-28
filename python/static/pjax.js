/* ===== SVN Web —— 轻量 PJAX 引擎（无 jQuery / 无外部依赖） =====
 *
 * 配合 NProgress 呈现顶部加载进度条，站内 <a> 跳转改为局部刷新：
 *  1. 点击站内链接 → 阻止默认跳转，fetch 目标页 HTML；
 *  2. 抽取返回 HTML 中 <main id="pjax-main"> 的内容替换当前 <main>；
 *  3. 同步 <title> 与 URL（history.pushState），并记录 state 支持前进/后退；
 *  4. 重新执行新内容里的 <script> 与 <link>（innerHTML 不会自动执行脚本）。
 *
 * 不接管：站外链接、target=_blank、download、data-no-pjax、同页链接。
 * 页面通过给 <main> 加 id="pjax-main" 标记要替换的区域（见 base.html）。
 */
(function () {
  'use strict';

  var MAIN = '#pjax-main';

  // 是否应被 pjax 接管
  function canPjax(a) {
    if (!a || !a.href) return false;
    if (a.getAttribute('target') === '_blank') return false;              // 新窗口
    if (a.hasAttribute('download')) return false;                         // 下载
    if (a.hasAttribute('data-no-pjax')) return false;                     // 显式排除
    if (a.origin !== window.location.origin) return false;                // 站外
    if (a.protocol !== 'http:' && a.protocol !== 'https:') return false;  // 非 http(s)
    try {
      var u = new URL(a.href, window.location.origin);
      var samePath = u.pathname === window.location.pathname &&
                     u.search === window.location.search;
      return !samePath;                                                   // 同页交给浏览器
    } catch (e) { return false; }
  }

  // 用新页面 HTML 替换 <main id="pjax-main">，并同步标题。返回是否成功。
  function replaceMain(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var oldMain = document.querySelector(MAIN);
    var newMain = doc.querySelector(MAIN);
    if (!oldMain || !newMain) return false;
    oldMain.innerHTML = newMain.innerHTML;
    // 同步 <main> 的 class（如登录页 login-main 的居中布局），否则 pjax 跳转后
    // 延续旧页面的 class，版式错乱、刷新才恢复
    oldMain.className = newMain.className;
    var nt = doc.querySelector('title');
    if (nt) document.title = nt.textContent;
    rehydrateScripts(oldMain);
    return true;
  }

  // 重新执行新内容里的脚本与样式表
  function rehydrateScripts(main) {
    // 内联脚本
    Array.prototype.forEach.call(
      main.querySelectorAll('script:not([src])'),
      function (s) {
        var c = document.createElement('script');
        c.textContent = s.textContent;
        s.parentNode.replaceChild(c, s);
      });
    // 外部脚本
    Array.prototype.forEach.call(
      main.querySelectorAll('script[src]'),
      function (s) {
        var c = document.createElement('script');
        c.src = s.src;
        c.async = false;
        s.parentNode.replaceChild(c, s);
      });
    // 新样式表
    Array.prototype.forEach.call(
      main.querySelectorAll('link[rel="stylesheet"]'),
      function (l) {
        var href = l.getAttribute('href');
        if (href && !document.querySelector('link[data-href="' + href + '"]')) {
          var c = l.cloneNode();
          c.setAttribute('data-href', href);
          document.head.appendChild(c);
        }
      });
  }

  // NProgress 微调：隐藏右下角转圈，仅保留顶部进度条
  if (window.NProgress) {
    window.NProgress.configure({ showSpinner: false });
  }

  function setLoading(on) {
    if (on) document.body.classList.add('pjax-loading');
    else document.body.classList.remove('pjax-loading');
  }

  function abortLoading() {
    if (window.NProgress) NProgress.done();
    setLoading(false);
  }

  function fetchAndSwap(url, push) {
    if (window.NProgress) NProgress.start();
    setLoading(true);
    fetch(url, { headers: { 'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        // 非页面响应（zip 等附件 / 二进制）不进 pjax：清掉加载态后交给浏览器整页处理，
        // 否则 attachment 响应不触发页面卸载，蒙层和进度条会永远留在页面上
        var ct = (r.headers.get('Content-Type') || '').toLowerCase();
        if (ct.indexOf('text/html') === -1) {
          abortLoading();
          window.location.href = url;
          return;
        }
        if (window.NProgress) NProgress.set(0.7);
        return r.text();
      })
      .then(function (html) {
        if (!html) return;   // 上面附件分支已处理
        if (!replaceMain(html)) {
          abortLoading();    // 回退整页前必须清加载态，防止卡在蒙层
          window.location.href = url;
          return;
        }
        if (push) {
          window.history.pushState({ pjax: url }, '', url);
        }
        window.dispatchEvent(new CustomEvent('pjax:end', { detail: { url: url } }));
        if (window.NProgress) NProgress.done();
        setLoading(false);
        window.scrollTo(0, 0);
      })
      .catch(function () {
        abortLoading();
        window.location.href = url;     // 失败回退整页
      });
  }

  // 点击接管站内链接
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || !canPjax(a)) return;
    e.preventDefault();
    fetchAndSwap(a.href, true);
  });

  // 前进 / 后退（读 state 里的 pjax URL）
  window.addEventListener('popstate', function (e) {
    var url = e.state && e.state.pjax;
    if (typeof url !== 'string') return;
    fetchAndSwap(url, false);
  });
})();