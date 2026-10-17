/* Bluetooth interaction state: no navigation and no browser dependencies. */
(function (root) {
  'use strict';
  function explainError(message) {
    const detail = String(message || '设备未返回有效结果');
    const text = detail.toLowerCase();
    let title = '暂时无法连接录音卡';
    let advice = ['确认录音卡已开机并靠近电脑。', '关闭手机或其他应用对录音卡的连接，然后重试。'];
    if (/not found|no device|no ae20|未找到|未见设备/.test(text)) {
      title = '没有找到录音卡';
      advice = ['唤醒录音卡并开启设备蓝牙，靠近电脑后重新扫描。', '若设备通过 Type-C 连接，请安全弹出并拔线后再试。', '断开手机应用或其他电脑对录音卡的连接。'];
    } else if (/not authorized|unauthorized|未授权|privacy/.test(text)) {
      title = '蓝牙权限未开启';
      advice = ['在 macOS「系统设置 → 隐私与安全性 → 蓝牙」允许启动管理台的应用访问蓝牙。', '授权后重新启动管理台，再尝试连接。'];
    } else if (/powered off|turned off|蓝牙未开启/.test(text)) {
      title = '电脑蓝牙未开启';
      advice = ['在电脑系统设置中打开蓝牙。', '确认录音卡已开机，再重新扫描。'];
    } else if (/unsupported|系统.*控制器|当前进程/.test(text)) {
      title = '当前环境无法使用蓝牙';
      advice = ['在本机终端启动管理台，排除受限运行环境。', '确认电脑蓝牙可用，并检查系统蓝牙权限。'];
    } else if (/timeout|timed out|超时/.test(text)) {
      title = '连接等待超时';
      advice = ['将录音卡靠近电脑，唤醒设备后重试。', '断开手机或其他软件的连接；必要时重新开机录音卡。'];
    } else if (/bleak|ble python|蓝牙依赖|ble_python/.test(text)) {
      title = '蓝牙运行环境未就绪';
      advice = ['在已启用的 Python 环境安装 requirements-core.txt。', '若已有独立蓝牙环境，可通过 QS668_BLE_PYTHON 指定，然后重启管理台。'];
    } else if (/有任务|正在运行|in progress|busy/.test(text)) {
      title = '请等待当前操作结束';
      advice = ['等待当前传输、转写或实时采集结束后再连接。'];
    } else if (/failed to fetch|networkerror|连接中断|请求失败/.test(text)) {
      title = '无法联系管理台';
      advice = ['确认本地管理台仍在运行，刷新页面后重试。'];
    }
    return {title, advice, detail};
  }

  function createFlow({request, onState, onFiles, onDevices, onError, onComplete}) {
    let active = null;
    let busy = false;
    let lastRequest = null;
    let selectedAddress = '';
    let requestToken = 0;
    let restored = false;
    let lastFiles = null;
    const notify = value => onState?.(value);

    async function start(op, extra = {}) {
      if (busy) return;
      busy = true;
      restored = true;
      const token = ++requestToken;
      const payload = {op, ...(selectedAddress ? {address: selectedAddress} : {}), ...extra};
      lastRequest = {op, extra: {...extra}};
      notify({phase: op === 'scan' ? 'scanning' : op === 'connect' || op === 'list' ? 'connecting' : 'working', busy: true, op, message: ''});
      try {
        const result = await request(payload);
        if (token !== requestToken) return;
        if (!result.job_id) throw new Error('管理台没有返回有效任务，请刷新页面重试');
        active = {id: result.job_id, op, token};
      } catch (error) {
        if (token !== requestToken) return;
        busy = false;
        active = null;
        notify({phase: 'failed', busy: false, op, message: error.message});
        onError?.(explainError(error.message));
      }
    }

    async function update(tasks) {
      if (!active) {
        // A refresh can restore the last file list without opening old error dialogs.
        if (!restored && !busy) {
          restored = true;
          const previous = tasks.find(t => t.kind === 'ble' && t.status === 'done' && t.result?.list_ok);
          if (previous) onFiles?.(previous.result.files || [], {reveal: false, started: previous.started});
        }
        return;
      }
      const task = tasks.find(t => t.id === active.id);
      if (!task) return;
      if (task.status === 'running') {
        notify({phase: active.op === 'scan' ? 'scanning' : ['connect', 'list'].includes(active.op) ? 'connecting' : 'working', busy: true, op: active.op, message: task.message, log: task.new || [], elapsed: task.elapsed});
        return;
      }
      const finished = active;
      active = null;
      busy = false;
      if (task.status !== 'done') {
        const message = task.message || (task.new || []).at(-1) || '蓝牙操作未完成';
        notify({phase: 'failed', busy: false, op: finished.op, message, log: task.new || []});
        onError?.(explainError(message));
        return;
      }
      if (finished.op === 'scan') {
        const devices = task.result?.devices || [];
        onDevices?.(devices);
        if (devices.length === 0) {
          const message = '未找到录音卡';
          notify({phase: 'failed', busy: false, op: 'scan', message});
          onError?.(explainError(message));
        } else if (devices.length === 1) {
          selectedAddress = devices[0].address;
          // Discovery alone does not imply that the device accepts a connection.
          await start('connect', {address: selectedAddress});
        } else {
          notify({phase: 'choose', busy: false, op: 'scan', message: `找到 ${devices.length} 台录音卡，请选择要连接的设备。`});
        }
        return;
      }
      if (['connect', 'list'].includes(finished.op)) {
        if (!task.result?.connection_verified || !task.result?.list_ok) {
          const message = '未能确认连接并读取卡片文件，请重新连接。';
          notify({phase: 'failed', busy: false, op: finished.op, message});
          onError?.(explainError(message));
          return;
        }
        if (task.result.address) selectedAddress = task.result.address;
        lastFiles = task.result.files || [];
        notify({phase: 'ready', busy: false, op: finished.op, message: `连接验证成功，已读取 ${lastFiles.length} 条录音。`, log: task.new || []});
        onFiles?.(lastFiles, {reveal: true, started: task.started});
      } else {
        notify({phase: 'done', busy: false, op: finished.op, message: '操作已完成', log: task.new || []});
        onComplete?.(finished.op, task);
      }
    }
    return {start, update, select: address => {selectedAddress = address;},
      retry: () => lastRequest ? start(lastRequest.op, lastRequest.extra) : start('connect'),
      isBusy: () => busy};
  }
  root.TingxuBluetooth = {createFlow, explainError};
})(globalThis);
