#!/usr/bin/env python3
"""生成本地静态预览副本：完整 index.html + 末尾追加本地解锁脚本。

仅供「财务中心 › 对账结算（结算看板）」静态原型评审：无后端时登录锁会把
页面锁回登录壳，预览副本在末尾注入解锁脚本（屏蔽 lockWorkspace、隐藏登录
门、注入管理员 mock 身份、自动打开对账结算页）。正式源码
src/purchase_tool/web/index.html 不包含该脚本，合并时删除本目录即可。

用法（在仓库根目录执行）：
    python3 docs/prototypes/20260917-settlement-dashboard/make_preview.py
产物：docs/prototypes/20260917-settlement-dashboard/本地预览-结算看板.html
     （双击打开，建议 Chrome/Edge；Safari 的 file:// localStorage 受限）
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src/purchase_tool/web/index.html"
OUT_DIR = Path(__file__).resolve().parent
OUT = OUT_DIR / "本地预览-结算看板.html"

UNLOCK = """
<script>
/* ===== 静态原型本地预览专用（仅此副本，不进产品源码） =====
   无后端时：屏蔽登录锁、隐藏登录门、注入管理员 mock 身份、自动打开对账结算。
   身份每次 tick 强制覆盖——应用的引导流程会在解锁后才写入真实身份，
   若只在 authIdentity 为空时注入，会被随后的无权限身份覆盖而打不开页面。 */
(function () {
  var MOCK = {
    name: 'Jeff（原型预览）',
    roles: ['super_admin'],
    permissions: ['finance.access'],
    workspaceAccess: true,
  };
  function unlock() {
    try { window.lockWorkspace = function () {}; } catch (e) {}
    try { if (typeof lockWorkspace === 'function') lockWorkspace = function () {}; } catch (e) {}
    document.body.classList.remove('auth-locked');
    var gate = document.getElementById('authGate'); if (gate) gate.hidden = true;
    var shell = document.getElementById('appShell'); if (shell) shell.removeAttribute('inert');
    try { authIdentity = MOCK; authReady = true; } catch (e) {}
    try { if (window.authIdentity !== undefined) window.authIdentity = MOCK; } catch (e) {}
  }
  unlock();
  var n = 0;
  var timer = setInterval(function () {
    unlock();
    if (++n > 60) clearInterval(timer);
  }, 250);
  function openProto() { unlock(); try { openFeatureTab('settledashboard'); } catch (e) {} }
  window.addEventListener('load', openProto);
  setTimeout(openProto, 600);
  setTimeout(openProto, 1500);
  setTimeout(openProto, 3000);
})();
</script>
"""

html = SRC.read_text(encoding="utf-8")
pos = html.rfind("</body>")
if pos < 0:
    raise SystemExit("index.html 中找不到 </body>")
OUT.write_text(html[:pos] + UNLOCK + html[pos:], encoding="utf-8")
print(f"已生成 {OUT}")
