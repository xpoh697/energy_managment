/**
 * Energy Management Card (v11.9.337+)
 * Premium UI with Detailed Schedule (4x6 Grid)
 */

const MODE_COLORS = {
  'sale_pv': 'hsl(140, 60%, 45%)',         // Green
  'sale_pv_no_bat': 'hsl(35, 90%, 50%)',  // Orange
  'sale_pv_bat': 'hsl(5, 80%, 55%)',      // Red/Coral
  'buy': 'hsl(210, 80%, 50%)',            // Blue
  'stop_sale': 'hsl(0, 0%, 40%)',          // Dark Grey
  'bat_emergency': 'hsl(280, 70%, 50%)',   // Purple
  'no_pv_sale_no_bat': 'hsl(210, 10%, 30%)',// Slate
  'default': 'var(--secondary-text-color)'
};

const MODE_ICONS = {
  'sale_pv': 'mdi:sun-wireless',
  'sale_pv_no_bat': 'mdi:solar-power',
  'sale_pv_bat': 'mdi:battery-arrow-up',
  'buy': 'mdi:battery-arrow-down',
  'stop_sale': 'mdi:hand-back-right',
  'bat_emergency': 'mdi:alert-decagram',
  'no_pv_sale_no_bat': 'mdi:moon-waning-crescent',
  'default': 'mdi:help-circle'
};

const MODE_LABELS = {
  'sale_pv': 'sale_pv',
  'sale_pv_no_bat': 'sale_pv_no_bat',
  'sale_pv_bat': 'sale_pv_bat',
  'buy': 'buy',
  'stop_sale': 'stop_sale',
  'bat_emergency': 'emergency',
  'no_pv_sale_no_bat': 'night_wait'
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
          --card-bg: var(--ha-card-background, var(--card-background-color, #fff));
          --primary-text: var(--primary-text-color, #212121);
          --secondary-text: var(--secondary-text-color, #727272);
          --accent: #03a9f4;
          --font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        ha-card {
          padding: 24px;
          border-radius: 28px;
          background: var(--card-bg);
          box-shadow: 0 12px 48px rgba(0,0,0,0.12);
          font-family: var(--font-family);
          position: relative;
          overflow: hidden;
          min-width: 360px;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          margin-bottom: 28px;
        }
        .title-group { display: flex; flex-direction: column; }
        .title { font-size: 1.4rem; font-weight: 800; letter-spacing: -0.03em; color: var(--primary-text); }
        .subtitle { font-size: 0.85rem; color: var(--secondary-text); font-weight: 600; opacity: 0.8; }
        .status-badge {
          background: rgba(0,0,0,0.03);
          padding: 8px 16px;
          border-radius: 16px;
          font-size: 0.8rem;
          font-weight: 800;
          text-transform: uppercase;
          border: 2px solid rgba(0,0,0,0.05);
        }

        .hero-section {
          display: flex;
          align-items: center;
          gap: 32px;
          margin-bottom: 32px;
          background: rgba(0,0,0,0.02);
          padding: 20px;
          border-radius: 24px;
        }
        .gauge-wrap {
          position: relative;
          width: 140px; height: 140px;
        }
        .gauge-svg { transform: rotate(-90deg); width: 100%; height: 100%; }
        .gauge-track { fill: none; stroke: rgba(0,0,0,0.05); stroke-width: 8; }
        .gauge-bar { 
          fill: none; stroke: var(--accent); stroke-width: 10; 
          stroke-linecap: round; transition: stroke-dashoffset 1.5s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        .gauge-label {
          position: absolute;
          top: 50%; left: 50%; transform: translate(-50%, -50%);
          display: flex; flex-direction: column; align-items: center;
        }
        .soc-value { font-size: 2.5rem; font-weight: 900; line-height: 1; color: var(--primary-text); }
        .soc-unit { font-size: 0.8rem; font-weight: 700; color: var(--secondary-text); }

        .stats-grid { flex: 1; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
        .stat-card {
          background: var(--card-bg);
          padding: 14px;
          border-radius: 18px;
          border: 1px solid rgba(0,0,0,0.04);
          box-shadow: 0 4px 12px rgba(0,0,0,0.02);
        }
        .stat-label { font-size: 0.65rem; font-weight: 800; color: var(--secondary-text); text-transform: uppercase; margin-bottom: 4px; display: block; }
        .stat-value { font-size: 1.1rem; font-weight: 800; color: var(--primary-text); }
        
        .timeline-section { margin-bottom: 32px; }
        .timeline-info { display: flex; justify-content: space-between; margin-bottom: 16px; font-size: 0.9rem; font-weight: 800; }
        .timeline-grid {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 12px;
        }
        .hour-bar {
          background: rgba(0,0,0,0.03);
          border-radius: 18px;
          padding: 12px 8px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: space-between;
          min-height: 90px;
          transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
          border: 2px solid transparent;
          cursor: help;
        }
        .hour-bar:hover { transform: translateY(-4px); box-shadow: 0 8px 24px rgba(0,0,0,0.12); z-index: 2; }
        .hour-bar.active { border-color: var(--accent); box-shadow: 0 0 0 4px rgba(3, 169, 244, 0.2); transform: scale(1.02); }
        
        .h-icon { --mdc-icon-size: 24px; color: white; margin-bottom: 4px; }
        .h-time { font-size: 0.9rem; font-weight: 900; color: white; line-height: 1; }
        .h-prices { display: flex; gap: 6px; margin: 6px 0; }
        .price-buy { font-size: 0.65rem; font-weight: 800; color: #90caf9; }
        .price-sell { font-size: 0.65rem; font-weight: 800; color: #a5d6a7; }
        .h-mode { font-size: 0.52rem; font-weight: 800; text-transform: uppercase; color: rgba(255,255,255,0.9); text-align: center; word-break: break-all; }

        .controls { display: flex; gap: 12px; overflow-x: auto; padding-bottom: 8px; scrollbar-width: none; }
        .controls::-webkit-scrollbar { display: none; }
        .btn {
          flex: 1 0 auto;
          background: rgba(0,0,0,0.03);
          border: 2px solid rgba(0,0,0,0.05);
          border-radius: 18px;
          padding: 14px 20px;
          font-size: 0.9rem;
          font-weight: 800;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 10px;
          color: var(--primary-text);
        }
        .btn:hover { background: var(--accent); color: white; border-color: var(--accent); transform: translateY(-2px); }
        .btn ha-icon { --mdc-icon-size: 22px; }
      </style>
      <ha-card>
        <div class="header">
          <div class="title-group">
            <div class="title">Energy Management</div>
            <div class="subtitle">Next-Gen Hybrid Controller</div>
          </div>
          <div id="status-badge" class="status-badge">AI Operational</div>
        </div>

        <div class="hero-section">
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 100 100">
              <circle class="gauge-track" cx="50" cy="50" r="42"></circle>
              <circle id="gauge-bar" class="gauge-bar" cx="50" cy="50" r="42" stroke-dasharray="264" stroke-dashoffset="264"></circle>
            </svg>
            <div class="gauge-label">
              <span id="soc-val" class="soc-value">--</span>
              <span class="soc-unit">SOC %</span>
            </div>
          </div>
          
          <div class="stats-grid">
            <div class="stat-card">
              <span class="stat-label">Morning Projection</span>
              <span id="proj-morning" class="stat-value">-- %</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">Safe Export Until</span>
              <span id="limit-h" class="stat-value">--:00</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">Power Dispatch</span>
              <span id="power-now" class="stat-value">0.0 kW</span>
            </div>
            <div class="stat-card">
              <span class="stat-label">Version</span>
              <span id="v-code" class="stat-value">--</span>
            </div>
          </div>
        </div>

        <div class="timeline-section">
          <div class="timeline-info">
            <span>24h Advanced Schedule</span>
            <span id="now-time">--:--</span>
          </div>
          <div id="timeline-grid" class="timeline-grid">
            ${Array(24).fill().map((_, i) => `
              <div class="hour-bar" id="h-bar-${i}">
                <ha-icon class="h-icon" id="h-icon-${i}" icon="mdi:help-circle"></ha-icon>
                <span class="h-time">${i < 10 ? '0'+i : i}:00</span>
                <div class="h-prices">
                  <span class="price-buy" id="h-buy-${i}">0.00</span>
                  <span class="price-sell" id="h-sell-${i}">0.00</span>
                </div>
                <span class="h-mode" id="h-mode-${i}">--</span>
              </div>
            `).join('')}
          </div>
        </div>

        <div class="controls">
          <button class="btn" onclick="this.getRootNode().host._callService('force_buy')">
            <ha-icon icon="mdi:lightning-bolt"></ha-icon> Force Buy
          </button>
          <button class="btn" onclick="this.getRootNode().host._callService('stop_sale')">
            <ha-icon icon="mdi:hand-back-right"></ha-icon> Stop Sale
          </button>
          <button class="btn" onclick="this.getRootNode().host._callService('ai_mode')">
            <ha-icon icon="mdi:robot"></ha-icon> AI Mode
          </button>
        </div>
      </ha-card>
    `;
    this._initialized = true;
  }

  _updateUI() {
    const entityId = this._config.entity || 'sensor.energy_management';
    const stateObj = this._hass.states[entityId];
    
    if (!stateObj) {
      console.warn(`EnergyManagementCard: Entity ${entityId} not found`);
      return;
    }

    const attrs = stateObj.attributes;
    const soc = parseFloat(attrs.battery_soc) || 0;
    const bms = attrs.bms_status || {};
    const projMorning = bms.proj_morning || attrs.morning_soc_projected || '--';
    const limitH = bms.limit_h || '--';
    const currentMode = stateObj.state;
    const hourlyData = attrs.hourly_data || {};
    const power = parseFloat(attrs.power) || 0;
    const version = bms.v || 'v11.9.337';

    // Update Gauge
    const bar = this.shadowRoot.getElementById('gauge-bar');
    if (bar) {
      const offset = 264 - (264 * Math.min(100, Math.max(0, soc))) / 100;
      bar.style.strokeDashoffset = offset;
    }
    
    const socText = this.shadowRoot.getElementById('soc-val');
    if (socText) socText.innerText = Math.round(soc);

    // Update Status
    const badge = this.shadowRoot.getElementById('status-badge');
    if (badge) {
      badge.innerText = (currentMode || 'Unknown').replace(/_/g, ' ');
      const baseColor = MODE_COLORS[currentMode] || MODE_COLORS.default;
      badge.style.color = baseColor;
      badge.style.borderColor = baseColor.replace('50%', '30%');
    }

    // Update Stats
    const projText = this.shadowRoot.getElementById('proj-morning');
    if (projText) {
      const val = parseFloat(projMorning);
      projText.innerText = isNaN(val) ? projMorning : val.toFixed(1) + '%';
    }

    const limitText = this.shadowRoot.getElementById('limit-h');
    if (limitText) limitText.innerText = (limitH !== 'N/A' && limitH !== '--') ? limitH + ':00' : '--';

    const powerText = this.shadowRoot.getElementById('power-now');
    if (powerText) powerText.innerText = power.toFixed(1) + ' kW';

    const vText = this.shadowRoot.getElementById('v-code');
    if (vText) vText.innerText = version;

    // Update Timeline
    const nowHour = new Date().getHours();
    const timeText = this.shadowRoot.getElementById('now-time');
    if (timeText) timeText.innerText = `Current Time: ${nowHour}:00`;

    for (let i = 0; i < 24; i++) {
      const hStr = `${i < 10 ? '0'+i : i}:00`;
      const data = hourlyData[hStr] || { mode: 'default', buy_price: 0, sell_price: 0 };
      
      const cell = this.shadowRoot.getElementById(`h-bar-${i}`);
      const icon = this.shadowRoot.getElementById(`h-icon-${i}`);
      const buyText = this.shadowRoot.getElementById(`h-buy-${i}`);
      const sellText = this.shadowRoot.getElementById(`h-sell-${i}`);
      const modeText = this.shadowRoot.getElementById(`h-mode-${i}`);

      if (cell) {
        cell.style.background = MODE_COLORS[data.mode] || MODE_COLORS.default;
        cell.className = 'hour-bar' + (i === nowHour ? ' active' : '');
      }
      if (icon) icon.icon = MODE_ICONS[data.mode] || MODE_ICONS.default;
      if (buyText) buyText.innerText = data.buy_price.toFixed(2);
      if (sellText) sellText.innerText = data.sell_price.toFixed(2);
      if (modeText) modeText.innerText = MODE_LABELS[data.mode] || data.mode;
    }
  }

  _callService(action) {
    this._hass.callService('energy_management', action, {});
  }

  getCardSize() { return 8; }
}

customElements.define('energy-management-card', EnergyManagementCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-management-card",
  name: "Energy Management Card",
  preview: true,
  description: "A premium card for monitoring and controlling your inverter energy modes."
});
