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
          padding: 10px 4px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          min-height: 105px;
          transition: all 0.2s;
          border: 1px solid transparent;
          text-shadow: 0 1px 2px rgba(0,0,0,0.5);
        }
        .hour-bar.active { border-style: dashed; border-color: white; box-shadow: 0 0 15px rgba(255,255,255,0.1); }
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
            <div class="stat-card"><span class="stat-label">System State</span><span id="v-code" class="stat-value">--</span></div>
          </div>
        </div>

        <div class="controls">
          <button class="btn ${this._state.state === 'buy' ? 'active' : ''}" onclick="this.getRootNode().host._callService('force_buy')"><ha-icon icon="mdi:lightning-bolt"></ha-icon> Force Buy</button>
          <button class="btn ${this._state.state === 'stop_sale' ? 'active' : ''}" onclick="this.getRootNode().host._callService('stop_sale')"><ha-icon icon="mdi:hand-back-right"></ha-icon> Stop Sale</button>
          <button class="btn ${(['buy', 'stop_sale'].indexOf(this._state.state) === -1) ? 'active' : ''}" onclick="this.getRootNode().host._callService('ai_mode')"><ha-icon icon="mdi:robot"></ha-icon> AI Mode</button>
        </div>

        <div id="timeline-container">
          <!-- Dynamic sections TODAY / TOMORROW will be here -->
        </div>
      </ha-card>
    `;
    this._initialized = true;
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
    if (bar) bar.style.strokeDashoffset = 264 - (264 * Math.min(100, Math.max(0, soc))) / 100;
    this.shadowRoot.getElementById('soc-val').innerText = Math.round(soc);
    this.shadowRoot.getElementById('proj-morning').innerText = (parseFloat(bms.proj_morning) || 0).toFixed(1) + '%';
    this.shadowRoot.getElementById('limit-h').innerText = bms.limit_h ? bms.limit_h + ':00' : '--';
    this.shadowRoot.getElementById('power-now').innerText = (parseFloat(attrs.power) || 0).toFixed(1) + ' kW';
    this.shadowRoot.getElementById('v-code').innerText = bms.v || 'v11.9.341';

    // Update Status Badge
    const badge = this.shadowRoot.getElementById('status-badge');
    badge.innerText = stateObj.state.replace(/_/g, ' ');
    const color = MODE_COLORS[stateObj.state] || MODE_COLORS.default;
    badge.style.color = color;
    badge.style.borderColor = color;

    // Update Sliding Timeline
    this._renderTimeline(hourlyData);
  }

  _renderTimeline(data) {
    const container = this.shadowRoot.getElementById('timeline-container');
    const now = new Date();
    const currentHour = now.getHours();
    const todayStr = now.toISOString().split('T')[0];

    const sortedKeys = Object.keys(data).sort();
    const startIndex = sortedKeys.findIndex(k => k.includes(todayStr) && k.includes(`${currentHour < 10 ? '0' + currentHour : currentHour}:00`));

    if (startIndex === -1) return;

    const windowKeys = sortedKeys.slice(startIndex, startIndex + 24);
    let html = '';
    let currentDayLabel = '';

    const hexToRgba = (hex, alpha) => {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    };

    windowKeys.forEach((key, idx) => {
      const isTomorrow = !key.includes(todayStr);
      const label = isTomorrow ? 'TOMORROW' : 'TODAY';
      const timeOnly = key.split(' ')[1];
      const hourData = data[key];

      if (label !== currentDayLabel) {
        if (currentDayLabel !== '') html += '</div>'; // close previous grid
        html += `<div class="section-header">${label}</div><div class="timeline-grid">`;
        currentDayLabel = label;
      }

      const modeColor = MODE_COLORS[hourData.mode] || MODE_COLORS.default;
      const bgColor = hexToRgba(modeColor, 0.1);
      html += `
        <div class="hour-bar ${idx === 0 ? 'active' : ''}" style="border-color: ${modeColor}; background-color: ${bgColor};">
          <ha-icon class="h-icon" style="color:white" icon="${MODE_ICONS[hourData.mode] || MODE_ICONS.default}"></ha-icon>
          <span class="h-time">${timeOnly}</span>
          <div class="h-prices">
            <span class="price-buy">${hourData.buy_price.toFixed(2)}</span>
            <span class="price-sell">${hourData.sell_price.toFixed(2)}</span>
          </div>
          <span class="h-mode" style="color:${modeColor}">${MODE_LABELS[hourData.mode] || hourData.mode}</span>
          <div style="display:flex; flex-direction:column; align-items:center; margin-top:4px">
            <span class="h-soc" style="color:${modeColor}">${hourData.soc !== undefined ? 'SOC ' + hourData.soc.toFixed(2) + '%' : ''}</span>
          </div>
        </div>
      `;
    });
    html += '</div>';

    if (container.innerHTML !== html) {
      container.innerHTML = html;
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
