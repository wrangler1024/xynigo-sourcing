#!/usr/bin/env python3
"""生成本地静态预览副本：完整 index.html + 末尾追加本地解锁脚本。

仅供「店铺授权」静态原型评审：无后端时登录锁会把页面锁回登录壳，
预览副本在末尾注入解锁脚本（屏蔽 lockWorkspace、隐藏登录门、注入管理员
mock 身份、自动打开店铺授权页）。正式源码 src/purchase_tool/web/index.html
不包含该脚本，合并时删除本目录即可。

用法（在仓库根目录执行）：
    python3 docs/prototypes/20260917-store-auth/make_preview.py
产物：docs/prototypes/20260917-store-auth/本地预览-店铺授权.html（双击打开，建议 Chrome/Edge）
"""
from pathlib import Path
import shutil

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src/purchase_tool/web/index.html"
OUT_DIR = Path(__file__).resolve().parent
OUT = OUT_DIR / "本地预览-店铺授权.html"

UNLOCK = """
<script>
/* ===== 静态原型本地预览专用（仅此副本，不进产品源码） =====
   无后端时：屏蔽登录锁、隐藏登录门、注入管理员 mock 身份、自动打开店铺授权。 */
(function () {
  function unlock() {
    try { window.lockWorkspace = function () {}; } catch (e) {}
    try { lockWorkspace = function () {}; } catch (e) {}
    document.body.classList.remove('auth-locked');
    var gate = document.getElementById('authGate'); if (gate) gate.hidden = true;
    var shell = document.getElementById('appShell'); if (shell) shell.removeAttribute('inert');
    try {
      if (!authIdentity) {
        authIdentity = { name: 'Jeff（原型预览）', roles: ['super_admin'], permissions: [], workspaceAccess: true };
        authReady = true;
      }
    } catch (e) {}
  }
  unlock();
  var n = 0;
  var timer = setInterval(function () { unlock(); if (++n > 40) { clearInterval(timer); } }, 250);
  function openProto() { unlock(); try { openFeatureTab('sheinstoreauth'); } catch (e) {} }
  window.addEventListener('load', openProto);
  setTimeout(openProto, 800);
  setTimeout(openProto, 2000);
})();
</script>
"""

html = SRC.read_text(encoding="utf-8")
pos = html.rfind("</body>")
if pos < 0:
    raise SystemExit("index.html 中找不到 </body>")
OUT.write_text(html[:pos] + UNLOCK + html[pos:], encoding="utf-8")

# 复制页面引用的静态资源，保证 file:// 直开时图标完整
for f in SRC.parent.iterdir():
    if f.suffix.lower() in {".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp"}:
        shutil.copy2(f, OUT_DIR / f.name)

print(f"OK {OUT} ({OUT.stat().st_size} bytes)")
