/**
 * Energy Management Card (v11.9.333+)
 * Premium UI for Home Assistant Energy Management Integration
 */

const MODE_COLORS = {
  'sale_pv': 'hsl(45, 100%, 50%)',         // Gold
  'sale_pv_no_bat': 'hsl(30, 100%, 50%)',  // Orange
  'buy': 'hsl(210, 100%, 50%)',            // Blue
  'stop_sale': 'hsl(0, 100%, 50%)',         // Red
  'bat_emergency': 'hsl(280, 100%, 50%)',   // Purple
  'no_pv_sale_no_bat': 'hsl(200, 15%, 50%)',// Slate
  'default': 'var(--secondary-text-color)'
};

const MODE_LABELS = {
  'sale_pv': 'Sale PV',
  'sale_pv_no_bat': 'Export PV (No Bat)',
  'buy': 'Grid Charging',
  'stop_sale': 'Stop Sale',
  'bat_emergency': 'Emergency',
  'no_pv_sale_no_bat': 'Night Wait'
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
          padding: 20px;
          border-radius: 24px;
          background: var(--card-bg);
          box-shadow: 0 8px 32px rgba(0,0,0,0.08);
          font-family: var(--font-family);
          position: relative;
          overflow: hidden;
        }
        .glass-overlay {
          position: absolute;
          top: -50%; left: -50%; width: 200%; height: 200%;
          background: radial-gradient(circle, rgba(255,255,255,0.05) 0%, transparent 70%);
          pointer-events: none;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          margin-bottom: 24px;
        }
        .title-group {
          display: flex;
          flex-direction: column;
        }
        .title {
          font-size: 1.25rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          color: var(--primary-text);
        }
        .subtitle {
          font-size: 0.8rem;
          color: var(--secondary-text);
          font-weight: 500;
        }
        .status-badge {
          background: rgba(0,0,0,0.04);
          padding: 6px 14px;
          border-radius: 14px;
          font-size: 0.75rem;
          font-weight: 700;
          letter-spacing: 0.05em;
          text-transform: uppercase;
          border: 1px solid rgba(0,0,0,0.05);
        }

        .hero-section {
          display: flex;
          align-items: center;
          gap: 24px;
          margin-bottom: 28px;
        }
        .gauge-wrap {
          position: relative;
          width: 130px; height: 130px;
          filter: drop-shadow(0 4px 12px rgba(3, 169, 244, 0.2));
        }
        .gauge-svg { transform: rotate(-90deg); width: 100%; height: 100%; }
        .gauge-track { fill: none; stroke: rgba(0,0,0,0.05); stroke-width: 10; }
        .gauge-bar { 
          fill: none; stroke: var(--accent); stroke-width: 10; 
          stroke-linecap: round; transition: stroke-dashoffset 1.5s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        .gauge-label {
          position: absolute;
          top: 50%; left: 50%; transform: translate(-50%, -50%);
          display: flex; flex-direction: column; align-items: center;
        }
        .soc-value { font-size: 2.2rem; font-weight: 800; line-height: 1; color: var(--primary-text); }
        .soc-unit { font-size: 0.8rem; font-weight: 600; color: var(--secondary-text); margin-top: 2px; }

        .stats-grid {
          flex: 1;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 12px;
        }
        .stat-card {
          background: rgba(0,0,0,0.02);
          padding: 12px;
          border-radius: 16px;
          border: 1px solid rgba(0,0,0,0.03);
          display: flex;
          flex-direction: column;
        }
        .stat-label { font-size: 0.65rem; font-weight: 700; color: var(--secondary-text); text-transform: uppercase; margin-bottom: 4px; }
        .stat-value { font-size: 1rem; font-weight: 700; color: var(--primary-text); }
        
        .timeline-section { margin-bottom: 24px; }
        .timeline-info { display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 0.85rem; font-weight: 600; }
        .timeline-grid {
          display: grid;
          grid-template-columns: repeat(24, 1fr);
          gap: 3px;
          height: 16px;
          background: rgba(0,0,0,0.03);
          border-radius: 8px;
          padding: 4px;
        }
        .hour-bar {
          border-radius: 4px;
          transition: all 0.3s ease;
          position: relative;
        }
        .hour-bar:hover { transform: scaleY(1.4); filter: brightness(1.1); z-index: 10; }
        .hour-bar.active {
          box-shadow: 0 0 8px rgba(0,0,0,0.2);
          transform: scaleY(1.2);
          animation: pulse 2s infinite;
        }

        .controls {
          display: flex;
          gap: 10px;
          overflow-x: auto;
          padding-bottom: 4px;
          scrollbar-width: none;
        }
        .controls::-webkit-scrollbar { display: none; }
        .btn {
          flex: 0 0 auto;
          background: rgba(0,0,0,0.03);
          border: 1px solid rgba(0,0,0,0.05);
          border-radius: 14px;
          padding: 10px 16px;
          font-size: 0.85rem;
          font-weight: 700;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          gap: 8px;
          color: var(--primary-text);
        }
        .btn:hover { background: rgba(0,0,0,0.08); transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .btn ha-icon { --mdc-icon-size: 20px; }

        @keyframes pulse {
          0% { box-shadow: 0 0 0 0 rgba(0,0,0,0.1); }
          70% { box-shadow: 0 0 0 6px rgba(0,0,0,0); }
          100% { box-shadow: 0 0 0 0 rgba(0,0,0,0); }
        }
      </style>
      <ha-card>
        <div class="glass-overlay"></div>
        <div class="header">
          <div class="title-group">
            <div class="title">Energy Management</div>
            <div class="subtitle">Smart Inverter Controller</div>
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
              <span class="stat-label">System State</span>
              <span id="v-code" class="stat-value">v11.9.333</span>
            </div>
          </div>
        </div>

        <div class="timeline-section">
          <div class="timeline-info">
            <span>24h Mode Schedule</span>
            <span id="now-time">--:--</span>
          </div>
          <div id="timeline-grid" class="timeline-grid">
            ${Array(24).fill().map((_, i) => `<div class="hour-bar" data-hour="${i}"></div>`).join('')}
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
          <button class="btn" onclick="this.getRootNode().host._callService('reset_bms_profile')">
            <ha-icon icon="mdi:refresh"></ha-icon> Reset BMS
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
    const projMorning = attrs.bms_status ? attrs.bms_status.proj_morning : (attrs.morning_soc_projected || '--');
    const limitH = attrs.bms_status ? attrs.bms_status.limit_h : '--';
    const currentMode = stateObj.state;
    const plannedModes = attrs.planned_modes_24h || {};
    const power = parseFloat(attrs.power) || 0;

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
      badge.style.color = MODE_COLORS[currentMode] || MODE_COLORS.default;
      const baseColor = MODE_COLORS[currentMode] || MODE_COLORS.default;
      badge.style.borderColor = baseColor.replace('50%', '30%');
    }

    // Update Stats
    const projText = this.shadowRoot.getElementById('proj-morning');
    if (projText) {
      const val = parseFloat(projMorning);
      projText.innerText = isNaN(val) ? projMorning : val.toFixed(1) + '%';
    }

    const limitText = this.shadowRoot.getElementById('limit-h');
    if (limitText) limitText.innerText = limitH !== 'N/A' && limitH !== '--' ? limitH + ':00' : '--';

    const powerText = this.shadowRoot.getElementById('power-now');
    if (powerText) powerText.innerText = power.toFixed(1) + ' kW';

    // Update Timeline
    const grid = this.shadowRoot.getElementById('timeline-grid');
    const nowHour = new Date().getHours();
    
    const timeText = this.shadowRoot.getElementById('now-time');
    if (timeText) timeText.innerText = `Now: ${nowHour}:00`;

    // Map 24h forecast dictionary to grid
    for (let i = 0; i < 24; i++) {
      const hourStr = (i < 10 ? '0' + i : i) + ':00';
      const cell = grid.children[i];
      const modeStr = plannedModes[hourStr] || 'default';
      // extract actual mode name from "sale_pv (SP: 0.12): ..."
      const actualMode = modeStr.split(' ')[0].split(':')[0];
      
      cell.style.background = MODE_COLORS[actualMode] || MODE_COLORS.default;
      cell.className = 'hour-bar' + (i === nowHour ? ' active' : '');
      cell.title = `Time: ${hourStr}\nMode: ${actualMode}\nDetails: ${modeStr}`;
    }
  }

  _callService(action) {
    this._hass.callService('energy_management', action, {});
  }

  getCardSize() {
    return 4;
  }
}

customElements.define('energy-management-card', EnergyManagementCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-management-card",
  name: "Energy Management Card",
  preview: true,
  description: "A premium card for monitoring and controlling your inverter energy modes."
});
