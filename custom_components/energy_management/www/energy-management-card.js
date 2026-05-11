/**
 * Energy Management Card (v11.9.341+)
 * Sliding Window UI: Today/Tomorrow grouping and large buttons
 */

const MODE_COLORS = {
  'sale_pv': '#4caf50',            // Green (Normal)
  'sale_pv_no_bat': '#ff8c00',     // Orange (Export PV)
  'sale_pv_bat': '#ff4500',         // Red (Export Bat)
  'buy': '#2196f3',                 // Blue (Charging)
  'stop_sale': '#808080',           // Grey
  'bat_emergency': '#9400d3',      // Dark Violet
  'no_pv_sale_no_bat': '#4caf50',   // Green (Wait)
  'default': '#727272'
};

const MODE_ICONS = {
  'sale_pv': 'mdi:home-lightning-bolt',
  'sale_pv_no_bat': 'mdi:solar-power-variant',
  'sale_pv_bat': 'mdi:battery-arrow-up',
  'buy': 'mdi:battery-arrow-down',
  'stop_sale': 'mdi:hand-back-right',
  'bat_emergency': 'mdi:alert-decagram',
  'no_pv_sale_no_bat': 'mdi:home-clock',
  'default': 'mdi:help-circle'
};

const MODE_LABELS = {
  'sale_pv': 'Normal',
  'sale_pv_no_bat': 'Export PV',
  'sale_pv_bat': 'Discharge',
  'buy': 'Grid Charging',
  'stop_sale': 'Stop Sale',
  'bat_emergency': 'Emergency',
  'no_pv_sale_no_bat': 'Wait'
};

class EnergyManagementCard extends HTMLElement {
  constructor() {
    super();
    this._initialized = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized && this.shadowRoot) {
      this._updateContent();
    } else if (this._initialized) {
      this._updateUI();
    }
  }

  setConfig(config) {
    this._config = config;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      this._initLayout();
    }
  }

  _initLayout() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --card-bg: var(--ha-card-background, var(--card-background-color, #1a1a1a));
          --primary-text: var(--primary-text-color, #ffffff);
          --secondary-text: var(--secondary-text-color, #aaaaaa);
          --accent: #03a9f4;
          --font-family: 'Outfit', 'Inter', sans-serif;
          color-scheme: dark;
        }
        ha-card {
          padding: 24px;
          border-radius: 28px;
          background: var(--card-bg);
          box-shadow: 0 12px 48px rgba(0,0,0,0.3);
          font-family: var(--font-family);
          color: var(--primary-text);
        }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; }
        .title { font-size: 1.4rem; font-weight: 800; }
        .status-badge { padding: 8px 16px; border-radius: 16px; font-size: 0.8rem; font-weight: 800; text-transform: uppercase; border: 2px solid rgba(255,255,255,0.1); }

        .gauge-wrap { position: relative; width: 250px; height: 250px; flex-shrink: 0; margin-bottom: 24px; }
        .gauge-svg { transform: rotate(-90deg); width: 100%; height: 100%; overflow: visible; }
        .gauge-track { fill: none; stroke: rgba(255,255,255,0.05); stroke-width: 10; }
        .gauge-bar { fill: none; stroke: var(--accent); stroke-width: 16; stroke-linecap: round; transition: stroke-dashoffset 1s ease; }
        .gauge-label { position: absolute; top: 53%; left: 50%; transform: translate(-50%, -50%); display: flex; flex-direction: column; align-items: center; }
        .soc-value { font-size: 4rem; font-weight: 900; line-height: 0.7; letter-spacing: -0.04em; }
        .soc-unit { font-size: 1rem; font-weight: 700; color: var(--secondary-text); opacity: 0.7; margin-top: 4px; }
        .hero-section { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 20px; margin-bottom: 32px; background: rgba(255,255,255,0.03); padding: 32px; border-radius: 32px; }
        
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; width: 100%; max-width: 400px; }
        .stat-card { background: rgba(255,255,255,0.02); padding: 12px 16px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.05); }
        .stat-label { font-size: 0.65rem; font-weight: 800; color: var(--secondary-text); text-transform: uppercase; margin-bottom: 4px; display: block; opacity: 0.8; }
        .stat-value { font-size: 1.1rem; font-weight: 800; color: white; }

        .section-header { font-size: 0.9rem; font-weight: 900; color: #4dabf5; margin: 20px 0 12px; letter-spacing: 0.05em; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 6px; }
        .timeline-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(85px, 1fr)); gap: 8px; margin-bottom: 8px; }
        
        .hour-bar {
          border-radius: 14px;
          padding: 0;
          min-height: 105px;
          cursor: pointer;
          position: relative;
          background: transparent;
        }
        .bar-content {
          padding: 10px 4px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100%;
          width: 100%;
          border-radius: 14px;
          border: 1px solid transparent;
          text-shadow: 0 1px 2px rgba(0,0,0,0.5);
          transition: transform 0.2s ease, box-shadow 0.2s ease, filter 0.2s ease;
          box-sizing: border-box;
        }
        .hour-bar:hover .bar-content {
          transform: scale(1.05);
          box-shadow: 0 12px 30px rgba(0,0,0,0.7);
          filter: brightness(1.3);
          z-index: 10;
          border-color: rgba(255,255,255,0.4);
        }
        .hour-bar.active .bar-content { border-style: dashed; border-color: white; box-shadow: 0 0 15px rgba(255,255,255,0.1); }
        .h-icon { --mdc-icon-size: 22px; margin-bottom: 4px; }
        .h-time { font-size: 1.1rem; font-weight: 900; color: white; line-height: 1; }
        .h-prices { display: flex; gap: 8px; margin: 6px 0; }
        .price-buy { font-size: 0.75rem; font-weight: 800; color: #90caf9; }
        .price-sell { font-size: 0.75rem; font-weight: 800; color: #a5d6a7; }
        .h-mode { font-size: 0.65rem; font-weight: 800; text-align: center; line-height: 1; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.05em; }
        .h-soc { font-size: 0.55rem; font-weight: 700; color: rgba(255,255,255,0.7); margin-top: 2px; }

        .controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 12px; margin-top: 24px; }
        .btn {
          height: 52px;
          background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.1);
          border-radius: 16px;
          font-size: 0.85rem;
          font-weight: 800;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          color: white;
          white-space: nowrap;
        }
        .btn:hover { background: var(--accent); border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 4px 12px rgba(3, 169, 244, 0.3); }
        .btn.active { background: var(--accent); border-color: var(--accent); box-shadow: inset 0 2px 4px rgba(0,0,0,0.2); }
        .btn ha-icon { --mdc-icon-size: 20px; }

        /* Modal Styles */
        .modal-overlay {
          position: fixed; top: 0; left: 0; width: 100%; height: 100%;
          background: rgba(0,0,0,0.7); backdrop-filter: blur(8px);
          display: none; align-items: center; justify-content: center; z-index: 1000;
        }
        .modal-overlay.open { display: flex; }
        .modal-card {
          background: #1e1e1e; width: 95%; max-width: 380px;
          border-radius: 32px; padding: 32px; border: 1px solid rgba(255,255,255,0.1);
          box-shadow: 0 30px 80px rgba(0,0,0,0.6);
          color: white;
        }
        .modal-header { font-size: 1.4rem; font-weight: 900; margin-bottom: 28px; display: flex; justify-content: space-between; align-items: center; }
        .modal-close { cursor: pointer; opacity: 0.6; transition: opacity 0.2s; }
        .modal-close:hover { opacity: 1; }
        .modal-body { display: flex; flex-direction: column; gap: 24px; }
        .form-group { display: flex; flex-direction: column; gap: 10px; }
        .form-label { font-size: 0.8rem; font-weight: 900; color: #4dabf5; text-transform: uppercase; letter-spacing: 0.05em; }
        .modal-info-grid {
          background: rgba(255,255,255,0.05);
          border-radius: 12px;
          padding: 12px;
          margin-bottom: 20px;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .info-row {
          display: flex;
          justify-content: space-between;
          font-size: 14px;
          color: #aaa;
        }
        .info-row b {
          color: #fff;
          font-family: 'Roboto Mono', monospace;
        }
        
        select {
          background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
          border-radius: 16px; padding: 14px; color: white; font-family: inherit; font-size: 1.1rem;
          cursor: pointer; outline: none; appearance: none;
          background-image: url("data:image/svg+xml;charset=US-ASCII,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2224%22%20height%3D%2224%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22white%22%20stroke-width%3D%222%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpolyline%20points%3D%226%209%2012%2015%2018%209%22%3E%3C%2Fpolyline%3E%3C%2Fsvg%3E");
          background-repeat: no-repeat; background-position: right 14px center; background-size: 18px;
        }
        select option { background: #2a2a2a; color: white; padding: 10px; }
        
        input[type="range"] { width: 100%; height: 8px; border-radius: 4px; background: rgba(255,255,255,0.1); outline: none; -webkit-appearance: none; margin-top: 10px; }
        input[type="range"]::-webkit-slider-thumb { -webkit-appearance: none; width: 24px; height: 24px; background: #03a9f4; border-radius: 50%; cursor: pointer; box-shadow: 0 0 10px rgba(3,169,244,0.5); }

        .modal-footer { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 32px; }
        .btn-save { background: #03a9f4; color: white; border: none; box-shadow: 0 4px 15px rgba(3,169,244,0.3); }
        .btn-clear { background: rgba(255,255,255,0.05); color: #ff5252; border: 1px solid rgba(255,82,82,0.2); }
        .btn:active { transform: scale(0.95); }
      </style>
      <ha-card>
        <div class="header">
          <div class="title">Energy Management</div>
          <div id="status-badge" class="status-badge">AI Operational</div>
        </div>

        <div class="hero-section">
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 160 160">
              <circle class="gauge-track" cx="80" cy="80" r="72"></circle>
              <circle id="gauge-bar" class="gauge-bar" cx="80" cy="80" r="72" stroke-dasharray="452" stroke-dashoffset="452"></circle>
            </svg>
            <div class="gauge-label">
              <span id="soc-val" class="soc-value">--</span>
              <span class="soc-unit">SOC %</span>
            </div>
          </div>
          <div class="stats-grid">
            <div class="stat-card"><span class="stat-label">Morning Projection</span><span id="proj-morning" class="stat-value">-- %</span></div>
            <div class="stat-card"><span class="stat-label">Safe Export Until</span><span id="limit-h" class="stat-value">--:00</span></div>
            <div class="stat-card"><span class="stat-label">Power Dispatch</span><span id="power-now" class="stat-value">0.0 kW</span></div>
            <div class="stat-card"><span class="stat-label">System State</span><span id="v-code" class="stat-value">v11.9.370</span></div>
          </div>
        </div>

        <div class="controls">
          <button id="btn-buy" class="btn" onclick="this.getRootNode().host._callService('force_buy')"><ha-icon icon="mdi:lightning-bolt"></ha-icon> Force Buy</button>
          <button id="btn-stop" class="btn" onclick="this.getRootNode().host._callService('stop_sale')"><ha-icon icon="mdi:hand-back-right"></ha-icon> Stop Sale</button>
          <button id="btn-ai" class="btn" onclick="this.getRootNode().host._callService('ai_mode')"><ha-icon icon="mdi:robot"></ha-icon> AI Mode</button>
        </div>

        <div id="timeline-container">
          <!-- Dynamic sections TODAY / TOMORROW will be here -->
        </div>

        <!-- Hourly Modal -->
        <div id="modal" class="modal-overlay">
          <div class="modal-card">
            <div class="modal-header">
              <span id="modal-title">Edit Hour</span>
              <span class="modal-close" onclick="this.getRootNode().host._closeModal()"><ha-icon icon="mdi:close"></ha-icon></span>
            </div>
            <div class="modal-body">
              <div class="modal-info-grid">
                <div class="info-row"><span>Buy:</span><b id="info-buy">-</b></div>
                <div class="info-row"><span>Sell:</span><b id="info-sell">-</b></div>
              </div>
              <div class="form-group">
                <span class="form-label">Mode Override</span>
                <select id="modal-mode" onchange="this.getRootNode().host._toggleSocVisibility()">
                   <option value="ai">AI (Automatic)</option>
                  <option value="buy">Grid Charging</option>
                  <option value="sale_pv_bat">Discharge Battery</option>
                  <option value="sale_pv_no_bat">Export PV Only</option>
                  <option value="no_pv_sale_no_bat">System Wait (Idle)</option>
                  <option value="stop_sale">Stop Sale</option>
                  <option value="sale_pv">Normal (PV Only)</option>
                </select>
              </div>
              <div class="form-group" id="soc-group">
                <span class="form-label">SOC Target: <span id="modal-soc-label">100</span>%</span>
                <input type="range" id="modal-soc" min="0" max="100" value="100" oninput="this.getRootNode().host._updateSocLabel(this.value)">
              </div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-clear" onclick="this.getRootNode().host._saveOverride('ai')">Reset to AI</button>
              <button class="btn btn-save" onclick="this.getRootNode().host._saveOverride()">Save Changes</button>
            </div>
          </div>
        </div>
      </ha-card>
    `;
    this._initialized = true;
  }

  _updateSocLabel(val) {
    this.shadowRoot.getElementById('modal-soc-label').innerText = val;
  }

  _openModal(timestamp, currentMode) {
    const data = this._hass.states[this._config.entity].attributes.hourly_data || {};
    const hourData = data[timestamp] || {};
    const currentSocLimit = hourData.soc_limit !== undefined ? hourData.soc_limit : (hourData.soc || 100);

    this._editingTimestamp = timestamp;
    this.shadowRoot.getElementById('modal-title').innerText = timestamp;
    this.shadowRoot.getElementById('modal-mode').value = currentMode === 'ai' ? 'ai' : currentMode;
    
    const socSlider = this.shadowRoot.getElementById('modal-soc');
    if (socSlider) {
      socSlider.value = currentSocLimit;
      this._updateSocLabel(currentSocLimit);
    }

    // Fill Market Info (v11.9.383)
    const currency = this._hass.states[this._config.entity].attributes.unit_of_measurement || 'PLN';
    this.shadowRoot.getElementById('info-buy').innerText = `${hourData.buy_price || 0} ${currency}`;
    this.shadowRoot.getElementById('info-sell').innerText = `${hourData.sell_price || 0} ${currency}`;

    this._toggleSocVisibility();
    this.shadowRoot.getElementById('modal').classList.add('open');
  }

  _toggleSocVisibility() {
    const mode = this.shadowRoot.getElementById('modal-mode').value;
    const socGroup = this.shadowRoot.getElementById('soc-group');
    if (socGroup) {
      // STRICT: Show ONLY for Buy and Sale_PV_BAT. Hide for AI and everything else.
      const isVisible = (mode === 'buy' || mode === 'sale_pv_bat');
      socGroup.style.display = isVisible ? 'flex' : 'none';
    }
  }

  _closeModal() {
    this.shadowRoot.getElementById('modal').classList.remove('open');
  }

  async _saveOverride(forcedMode) {
    const mode = forcedMode || this.shadowRoot.getElementById('modal-mode').value;
    const soc = this.shadowRoot.getElementById('modal-soc').value;
    
    await this._hass.callService('energy_management', 'set_hourly_override', {
      timestamp: this._editingTimestamp,
      mode: mode,
      soc_limit: parseFloat(soc)
    });
    
    this._closeModal();
  }

  _updateUI() {
    const entityId = this._config.entity || 'sensor.energy_management';
    const stateObj = this._hass.states[entityId];
    if (!stateObj) return;

    const attrs = stateObj.attributes;
    const soc = parseFloat(attrs.battery_soc) || 0;
    const bms = attrs.bms_status || {};
    const hourlyData = attrs.hourly_data || {};

    // Update Gauge & Stats
    const bar = this.shadowRoot.getElementById('gauge-bar');
    if (bar) bar.style.strokeDashoffset = 452 - (452 * Math.min(100, Math.max(0, soc))) / 100;
    this.shadowRoot.getElementById('soc-val').innerText = Math.round(soc);
    this.shadowRoot.getElementById('proj-morning').innerText = (parseFloat(attrs.morning_soc_projected) || 0).toFixed(1) + '%';
    this.shadowRoot.getElementById('limit-h').innerText = attrs.next_peak_start_hour || '--:00';
    this.shadowRoot.getElementById('power-now').innerText = (parseFloat(attrs.power) || 0).toFixed(1) + ' kW';
    this.shadowRoot.getElementById('v-code').innerText = 'v11.9.389';

    const badge = this.shadowRoot.getElementById('status-badge');
    if (badge) {
        badge.innerText = stateObj.state.replace(/_/g, ' ');
        const color = MODE_COLORS[stateObj.state] || MODE_COLORS.default;
        badge.style.color = color;
        badge.style.borderColor = color;
    }

    const btnBuy = this.shadowRoot.getElementById('btn-buy');
    const btnStop = this.shadowRoot.getElementById('btn-stop');
    const btnAi = this.shadowRoot.getElementById('btn-ai');
    
    if (btnBuy) btnBuy.classList.toggle('active', stateObj.state === 'buy');
    if (btnStop) btnStop.classList.toggle('active', stateObj.state === 'stop_sale');
    if (btnAi) btnAi.classList.toggle('active', ['buy', 'stop_sale'].indexOf(stateObj.state) === -1);

    this._renderTimeline(hourlyData);
  }

  _renderTimeline(data) {
    const container = this.shadowRoot.getElementById('timeline-container');
    if (!container) return;
    
    const now = new Date();
    const todayStr = now.toISOString().split('T')[0];

    const currentHour = now.getHours();
    const sortedKeys = Object.keys(data).sort();
    if (sortedKeys.length === 0) return;

    // Find the index of the current hour for the "Sliding Window"
    const startIndex = sortedKeys.findIndex(k => k.includes(todayStr) && k.includes(`${currentHour < 10 ? '0' + currentHour : currentHour}:00`));
    
    // Fallback: if not found, show all (though it should be found)
    const windowKeys = startIndex !== -1 ? sortedKeys.slice(startIndex, startIndex + 24) : sortedKeys.slice(0, 24);

    const hexToRgba = (hex, alpha) => {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    };

    // Smart DOM Update Logic
    const currentKeysStr = windowKeys.join(',');
    if (container._lastKeys !== currentKeysStr) {
      // Structure changed (e.g., new hour started) -> Full rebuild
      let html = '';
      let currentDayLabel = '';
      windowKeys.forEach((key, idx) => {
        const isTomorrow = !key.includes(todayStr);
        const label = isTomorrow ? 'TOMORROW' : 'TODAY';
        const hourData = data[key];
        if (label !== currentDayLabel) {
          if (currentDayLabel !== '') html += '</div>';
          html += `<div class="section-header">${label}</div><div class="timeline-grid">`;
          currentDayLabel = label;
        }
        const modeColor = MODE_COLORS[hourData.mode] || MODE_COLORS.default;
        const bgColor = hexToRgba(modeColor, 0.1);
        html += `
          <div class="hour-bar ${idx === 0 ? 'active' : ''}" data-ts="${key}" data-mode="${hourData.mode}" id="hb-${key.replace(/[: ]/g, '-')}">
            <div class="bar-content" style="border-color: ${modeColor}; background-color: ${bgColor};">
              <ha-icon class="h-icon" style="color:${modeColor}" icon="${MODE_ICONS[hourData.mode] || MODE_ICONS.default}"></ha-icon>
              <span class="h-time">${key.split(' ')[1]}</span>
              <div class="h-prices">
                <span class="price-buy">${hourData.buy_price.toFixed(2)}</span>
                <span class="price-sell">${hourData.sell_price.toFixed(2)}</span>
              </div>
              <span class="h-mode" style="color:${modeColor}">${MODE_LABELS[hourData.mode] || hourData.mode}</span>
              <div style="display:flex; flex-direction:column; align-items:center; margin-top:4px">
                <span class="h-soc" style="color:${modeColor}">${hourData.soc !== undefined ? 'SOC ' + hourData.soc.toFixed(2) + '%' : ''}</span>
              </div>
            </div>
          </div>
        `;
      });
      html += '</div>';
      container.innerHTML = html;
      container._lastKeys = currentKeysStr;
      
      // Re-bind listeners
      container.querySelectorAll('.hour-bar').forEach(bar => {
        bar.addEventListener('click', () => this._openModal(bar.getAttribute('data-ts'), bar.getAttribute('data-mode')));
      });
    } else {
      // Structure same -> Point update to preserve hover states
      windowKeys.forEach(key => {
        const hourData = data[key];
        const bar = container.querySelector(`#hb-${key.replace(/[: ]/g, '-')}`);
        if (!bar) return;
        
        const modeColor = MODE_COLORS[hourData.mode] || MODE_COLORS.default;
        const content = bar.querySelector('.bar-content');
        const icon = bar.querySelector('.h-icon');
        const modeLabel = bar.querySelector('.h-mode');
        const socLabel = bar.querySelector('.h-soc');
        const buyPrice = bar.querySelector('.price-buy');
        const sellPrice = bar.querySelector('.price-sell');

        if (content) {
          content.style.borderColor = modeColor;
          content.style.backgroundColor = hexToRgba(modeColor, 0.1);
        }
        if (icon) {
          icon.style.color = modeColor;
          icon.icon = MODE_ICONS[hourData.mode] || MODE_ICONS.default;
        }
        if (modeLabel) {
          modeLabel.style.color = modeColor;
          modeLabel.innerText = MODE_LABELS[hourData.mode] || hourData.mode;
        }
        if (socLabel) {
          socLabel.style.color = modeColor;
          socLabel.innerText = hourData.soc !== undefined ? 'SOC ' + hourData.soc.toFixed(2) + '%' : '';
        }
        if (buyPrice) buyPrice.innerText = hourData.buy_price.toFixed(2);
        if (sellPrice) sellPrice.innerText = hourData.sell_price.toFixed(2);
        bar.setAttribute('data-mode', hourData.mode);
      });
    }
  }

  _callService(action) {
    this._hass.callService('energy_management', action, {});
  }

  getCardSize() { return 12; }
}

customElements.define('energy-management-card', EnergyManagementCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "energy-management-card", name: "Energy Management Card", preview: true });
