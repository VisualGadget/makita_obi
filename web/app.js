// Serial port and display only. Protocol, decoding and safety checks are the makita_tool Python
// package, which runs here in Pyodide, so the page and the command line tool share one implementation.

const PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v0.27.7/full/';
const PACKAGE_FILES = ['__init__', 'battery', 'frame', 'transport', 'web'];
const BAUD_RATE = 9600;
const BOOT_WAIT_MS = 2500;
const READ_TIMEOUT_MS = 4000;
const MONITOR_INTERVAL_MS = 5000;

const element = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Firmware connection over Web Serial. */
class SerialLink {
  constructor() {
    this.pending = [];
    this.waiter = null;
    this.onLost = () => {};
  }

  async open(onChosen) {
    this.port = await navigator.serial.requestPort();
    onChosen();
    await this.port.open({ baudRate: BAUD_RATE });
    try {
      await this.port.setSignals({ dataTerminalReady: false, requestToSend: false });
    } catch {
      // The board also works when the signals can not be set, it only restarts on opening.
    }

    this.pump();
    await wait(BOOT_WAIT_MS);
  }

  async pump() {
    this.reader = this.port.readable.getReader();
    try {
      for (;;) {
        const { value, done } = await this.reader.read();
        if (done) {
          break;
        }

        this.pending.push(...value);
        this.waiter?.();
      }
    } catch {
      // Handled below: an unplugged adapter ends the stream with an error.
    } finally {
      this.reader.releaseLock();
    }

    if (!this.closing) {
      this.onLost();
    }
  }

  /** Called from Python: drop pending input, send bytes and read a reply. */
  async exchange(dataProxy, length) {
    const data = Uint8Array.from(dataProxy.toJs());
    this.pending = [];
    const writer = this.port.writable.getWriter();
    try {
      await writer.write(data);
    } finally {
      writer.releaseLock();
    }

    const deadline = Date.now() + READ_TIMEOUT_MS;
    while (this.pending.length < length && Date.now() < deadline) {
      await new Promise((resolve) => {
        this.waiter = resolve;
        setTimeout(resolve, deadline - Date.now());
      });
    }

    this.waiter = null;

    return Uint8Array.from(this.pending.slice(0, length));
  }

  async close() {
    this.closing = true;
    await this.reader?.cancel();
    await this.port?.close();
  }
}

let pyodide = null;
let tool = null;
let link = null;
let busy = false;
let monitoring = false;
let backup = null;
let backupSaved = false;

function setStatus(text) {
  element('status').textContent = text;
}

function show(id, visible) {
  element(id).hidden = !visible;
}

function updateButtons() {
  const ready = tool !== null;
  show('install-ready', !ready && !busy);
  show('install-blocked', ready);
  element('read').disabled = !ready || busy || monitoring;
  element('monitor').disabled = !ready || (busy && !monitoring);
  element('plan').disabled = !ready || busy || monitoring;
  element('backup').disabled = !ready || busy;
  const confirmed = element('confirm-unlock').value.trim() === 'UNLOCK';
  element('unlock').disabled = !ready || busy || monitoring || !confirmed || !backupSaved;
}

/** Run one battery operation at a time: the bus can not share transactions. */
async function exclusive(operation) {
  if (busy) {
    return undefined;
  }

  busy = true;
  updateButtons();
  try {
    return await operation();
  } finally {
    busy = false;
    updateButtons();
  }
}

async function call(method, ...args) {
  if (tool === null) {
    return { error: 'The adapter is not connected.' };
  }

  return JSON.parse(await tool[method](...args));
}

function rowsFor(report) {
  const live = report.live;
  const lock = report.locked ? `LOCKED: ${report.lock_causes.join(', ')}` : 'unlocked';
  const source = report.model_reported ? 'reported by the pack' : 'estimated from capacity';
  const rows = [
    ['Model', report.model ? `${report.model} (${source})` : 'unknown'],
    ['Manufactured', report.manufactured],
    ['Capacity', `${report.capacity_ah.toFixed(1)} Ah`],
    ['Controller type', report.controller_type ?? 'unknown'],
    ['Charge cycles', report.cycle_count],
    ['Lock state', lock],
    ['Failure code', report.failure],
    ['Cell failure flag', report.cell_failure ? 'set' : 'clear'],
    ['Damage rating', `${report.damage_rating} of 7`],
  ];
  if (live.pack_voltage !== null) {
    rows.push(['Pack voltage', `${live.pack_voltage.toFixed(3)} V`]);
  }

  live.cell_voltages.forEach((voltage, index) => rows.push([`Cell ${index + 1}`, `${voltage.toFixed(3)} V`]));
  if (report.imbalance !== null) {
    rows.push(['Cell imbalance', `${report.imbalance.toFixed(3)} V`]);
  }

  [['Temperature 1', live.temperature_c], ['Temperature 2', live.temperature2_c]].forEach(([label, value]) => {
    if (value !== null) {
      rows.push([label, `${value.toFixed(1)} °C`]);
    }
  });
  [
    ['Health', report.health_rating, ' of 4'],
    ['Overdischarge', report.overdischarge_percent, ' %'],
    ['Overload', report.overload_percent, ' %'],
    ['Overdischarge events', report.overdischarge_events, ''],
    ['Overload events', report.overload_events, ''],
    ['Charge level (raw)', report.charge_level_raw, ''],
  ].forEach(([label, value, unit]) => {
    if (value !== null) {
      rows.push([label, `${value}${unit}`]);
    }
  });
  rows.push(['ROM id', report.rom], ['Frame', report.frame]);

  return rows;
}

function showReport(report) {
  show('report-error', Boolean(report.error));
  if (report.error) {
    element('report-error').textContent = report.error;
    show('report', false);
    show('report-warning', false);
    show('unlock-section', false);

    return;
  }

  const body = element('report').tBodies[0];
  body.replaceChildren(...rowsFor(report).map(([label, value]) => {
    const row = document.createElement('tr');
    const heading = document.createElement('th');
    const cell = document.createElement('td');
    heading.scope = 'row';
    heading.textContent = label;
    cell.textContent = value;
    cell.className = label === 'Frame' || label === 'ROM id' ? 'mono' : '';
    row.append(heading, cell);

    return row;
  }));
  show('report', true);
  show('report-warning', report.emulated);
  element('report-warning').textContent = report.emulated
    ? 'No ROM id and no live data. This frame is a fixed reply, not the state of this pack. Check it with a meter.'
    : '';
  show('unlock-section', report.locked && !report.emulated);
}

async function readBattery() {
  show('plan-result', false);
  show('unlock-result', false);
  showReport(await call('report'));
}

async function connect() {
  link = new SerialLink();
  link.onLost = () => disconnected('The adapter was disconnected.');
  setStatus('Choose the adapter in the browser dialog.');
  try {
    await link.open(() => setStatus('Connecting...'));
  } catch (error) {
    link = null;
    setStatus(error.name === 'NotFoundError' ? 'No port was chosen.' : `Could not open the port: ${error.message}`);

    return;
  }

  const webModule = pyodide.pyimport('makita_tool.web');
  tool = webModule.WebTool(link.exchange.bind(link));
  const versions = await call('firmware');
  if (versions.error) {
    await disconnect();
    element('firmware').open = true;
    setStatus('The makita_obi firmware did not answer. If this board does not have it yet, install it above.');

    return;
  }

  element('versions').textContent = `Firmware ${versions.firmware}, tool ${versions.tool}`;
  element('connect').textContent = 'Disconnect';
  setStatus('Connected.');
  updateButtons();
}

function disconnected(message) {
  monitoring = false;
  tool = null;
  link = null;
  element('monitor').textContent = 'Start monitor';
  element('monitor').setAttribute('aria-pressed', 'false');
  element('connect').textContent = 'Connect';
  setStatus(message);
  updateButtons();
}

async function disconnect() {
  await link?.close();
  disconnected('Disconnected.');
}

async function runMonitor() {
  while (monitoring) {
    await exclusive(async () => {
      const report = await call('monitor');
      showReport(report);
      if (report.error || report.emulated) {
        stopMonitor();
      }
    });
    await wait(MONITOR_INTERVAL_MS);
  }
}

function stopMonitor() {
  monitoring = false;
  element('monitor').textContent = 'Start monitor';
  element('monitor').setAttribute('aria-pressed', 'false');
  updateButtons();
}

function toggleMonitor() {
  if (monitoring) {
    stopMonitor();

    return;
  }

  monitoring = true;
  element('monitor').textContent = 'Stop monitor';
  element('monitor').setAttribute('aria-pressed', 'true');
  updateButtons();
  runMonitor();
}

async function planUnlock() {
  const plan = await call('plan_unlock');
  backup = plan.backup ?? null;
  backupSaved = false;
  element('confirm-unlock').value = '';
  show('plan-result', true);
  show('unlock-result', false);
  show('plan-error', Boolean(plan.error || plan.nothing_to_do));
  element('plan-error').textContent = plan.error ?? 'Nothing to do: the frame is valid and the failure code is clear.';
  const blockers = plan.blockers ?? [];
  element('blockers').replaceChildren(...blockers.map((text) => {
    const item = document.createElement('li');
    item.textContent = text;

    return item;
  }));
  show('blockers', blockers.length > 0);
  show('plan-ok', !plan.error && !plan.nothing_to_do && blockers.length === 0);
  element('plan-frame').textContent = plan.frame ? `Current frame: ${plan.frame}` : '';
}

function saveBackup() {
  const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
  const text = `${JSON.stringify({ ...backup, saved: stamp }, null, 2)}\n`;
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  anchor.download = `${backup.rom}-${stamp}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(anchor.href), 10000);
  backupSaved = true;
  updateButtons();
}

async function runUnlock() {
  const result = await call('unlock');
  const box = element('unlock-result');
  show('plan-result', false);
  show('unlock-result', true);
  box.className = `notice ${result.error ? 'error' : 'ok'}`;
  box.textContent = result.error
    ? `Unlock failed: ${result.error}`
    : `Unlocked. Failure code ${result.failure}, cycle count now ${result.cycle_count}. Frame: ${result.frame}`;
  element('confirm-unlock').value = '';
  backupSaved = false;
  if (!result.error) {
    showReport(await call('report'));
    show('unlock-section', true);
  }
}

async function loadTool() {
  const { loadPyodide } = await import(`${PYODIDE_URL}pyodide.mjs`);
  pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
  pyodide.FS.mkdirTree('/home/pyodide/makita_tool');
  await Promise.all(PACKAGE_FILES.map(async (name) => {
    const response = await fetch(`py/makita_tool/${name}.py`);
    if (!response.ok) {
      throw new Error(`could not load ${name}.py`);
    }

    pyodide.FS.writeFile(`/home/pyodide/makita_tool/${name}.py`, await response.text());
  }));
  pyodide.runPython('import sys\nsys.path.insert(0, "/home/pyodide")\nimport makita_tool.web');
}

async function start() {
  if (!('serial' in navigator)) {
    show('unsupported', true);
    element('connect').textContent = 'Connect';
    setStatus('Web Serial is not available.');
    return;
  }

  try {
    await loadTool();
  } catch (error) {
    setStatus(`Could not load the tool: ${error.message}`);

    return;
  }

  element('connect').textContent = 'Connect';
  element('connect').disabled = false;
  setStatus('Ready. Connect the adapter, then press Connect.');
}

element('connect').addEventListener('click', async () => {
  element('connect').disabled = true;
  await exclusive(tool ? disconnect : connect);
  element('connect').disabled = false;
});
element('read').addEventListener('click', () => exclusive(readBattery));
element('monitor').addEventListener('click', toggleMonitor);
element('plan').addEventListener('click', () => exclusive(planUnlock));
element('backup').addEventListener('click', saveBackup);
element('unlock').addEventListener('click', () => exclusive(runUnlock));
element('confirm-unlock').addEventListener('input', updateButtons);
start();
