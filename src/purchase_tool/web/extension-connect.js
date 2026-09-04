(() => {
  'use strict';

  const params = new URLSearchParams(location.search);
  const clientId = String(params.get('clientId') || '').trim().toLowerCase();
  const clientIdText = document.getElementById('clientId');
  const approve = document.getElementById('approve');
  const status = document.getElementById('status');

  function setStatus(message, tone = '') {
    status.textContent = message;
    status.dataset.tone = tone;
  }

  function sendToExtension(message) {
    return new Promise((resolve, reject) => {
      if (!globalThis.chrome?.runtime?.sendMessage) {
        reject(new Error('当前浏览器不支持插件连接，请使用安装了店小秘提单助手的 Chrome/Comet 打开此页'));
        return;
      }
      chrome.runtime.sendMessage(clientId, message, (response) => {
        const error = chrome.runtime.lastError;
        if (error) reject(new Error(error.message));
        else if (!response?.ok) reject(new Error(response?.error || '插件未确认连接'));
        else resolve(response);
      });
    });
  }

  if (!/^[a-p]{32}$/.test(clientId)) {
    clientIdText.textContent = '插件标识无效';
    approve.disabled = true;
    setStatus('请回到插件弹窗重新发起连接。', 'error');
    return;
  }
  clientIdText.textContent = clientId;

  approve.addEventListener('click', async () => {
    approve.disabled = true;
    setStatus('正在核验 Xynigo 登录态并建立连接…');
    try {
      const response = await fetch('/api/extension/pair/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientId }),
      });
      const payload = await response.json();
      if (!response.ok || !payload?.ok) {
        throw new Error(payload?.error || 'Xynigo 拒绝了插件连接');
      }
      await sendToExtension({
        type: 'xynigo-dxm:bridge-approved',
        apiBaseUrl: payload.apiBaseUrl,
        bridgeToken: payload.bridgeToken,
      });
      setStatus('连接成功，可以关闭此页并返回店小秘。', 'success');
      approve.textContent = '已连接';
      window.setTimeout(() => window.close(), 700);
    } catch (error) {
      approve.disabled = false;
      setStatus(error?.message || '连接失败，请重试', 'error');
    }
  });
})();
