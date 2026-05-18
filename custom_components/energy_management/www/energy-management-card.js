/**
 * Energy Management Card (v11.9.341+)
 * Sliding Window UI: Today/Tomorrow grouping and large buttons
 */

console.info(
  "%c ENERGY MANAGEMENT %c v12.1.28 ",
  "color: white; background: #007bff; font-weight: bold; border-radius: 4px 0 0 4px; padding: 2px 6px;",
  "color: white; background: #28a745; font-weight: bold; border-radius: 0 4px 4px 0; padding: 2px 6px;"
);

const MODE_COLORS = {
  'sale_pv': '#4caf50',            // Green (Normal)
  'sale_pv_no_bat': '#ff8c00',     // Orange (Export PV)
  'sale_pv_bat': '#ff4500',         // Red (Export Bat)
  'buy': '#2196f3',                 // Blue (Charging)
  'stop_sale': '#ffb300',           // Yellow/Amber
  'bat_emergency': '#9400d3',      // Dark Violet
  'no_pv_sale_no_bat': '#808080',   // Grey (Wait)
  'default': '#727272'
};

const MODE_ICONS = {
  'sale_pv': 'mdi:solar-power-variant',
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
  'buy': 'Charge',
  'stop_sale': 'Stop Sale',
  'bat_emergency': 'Emergency',
  'no_pv_sale_no_bat': 'Wait'
};

function getSocInfo(soc) {
  if (soc === undefined || soc === null) {
    return { icon: 'mdi:battery-unknown', color: 'rgba(255,255,255,0.2)', percent: '' };
  }

  const val = parseFloat(soc);
  let color = '#ff6b6b'; // Coral Red (Low)
  let icon = 'mdi:battery-20';

  if (val >= 75) {
    color = '#66bb6a'; // Fresh Green (High)
  } else if (val >= 60) {
    color = '#a5d6a7'; // Light Green (Medium-High)
  } else if (val >= 40) {
    color = '#ffe082'; // Amber Yellow (Medium)
  } else if (val >= 25) {
    color = '#ffb74d'; // Orange (Medium-Low)
  }

  if (val >= 95) icon = 'mdi:battery';
  else if (val >= 85) icon = 'mdi:battery-90';
  else if (val >= 75) icon = 'mdi:battery-80';
  else if (val >= 65) icon = 'mdi:battery-70';
  else if (val >= 55) icon = 'mdi:battery-60';
  else if (val >= 45) icon = 'mdi:battery-50';
  else if (val >= 35) icon = 'mdi:battery-40';
  else if (val >= 25) icon = 'mdi:battery-30';
  else if (val >= 15) icon = 'mdi:battery-20';
  else icon = 'mdi:battery-10';

  return { icon, color, percent: val.toFixed(0) };
}

class EnergyManagementCard extends HTMLElement {
  constructor() {
    super();
    this._initialized = false;
    this._activeTab = null;
  }

  _switchTab(tab) {
    this._activeTab = tab;
    const container = this.shadowRoot.getElementById('timeline-container');
    if (container) container._lastKeys = null;
    this._updateUI();
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
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
        .title { font-size: 1.1rem; font-weight: 800; opacity: 0.8; }
        .status-badge { 
          padding: 4px 10px; 
          border-radius: 10px; 
          font-size: 0.7rem; 
          font-weight: 800; 
          text-transform: uppercase; 
          border: 1px solid rgba(255,255,255,0.1); 
          background: rgba(255,255,255,0.02);
        }

        .stats-panel {
          background: rgba(255,255,255,0.02);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 24px;
          padding: 16px;
          margin-bottom: 24px;
          display: flex;
          flex-direction: column;
          gap: 16px;
        }

        .hero-row {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 10px;
        }
        
        .hero-badge {
          height: 64px;
          border-radius: 16px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.04);
          transition: all 0.4s ease;
          cursor: pointer;
        }
        .hero-badge:hover { background: rgba(255,255,255,0.08); transform: translateY(-2px); }
        .hero-val { font-size: 1.6rem; font-weight: 900; line-height: 1; }
        .hero-label { font-size: 0.65rem; font-weight: 800; opacity: 0.5; text-transform: uppercase; margin-top: 4px; }

        .stats-grid { 
          display: grid; 
          grid-template-columns: repeat(auto-fit, minmax(85px, 1fr)); 
          gap: 8px; 
          width: 100%; 
        }
        .stat-card { 
          background: rgba(255,255,255,0.02); 
          padding: 8px 10px; 
          border-radius: 12px; 
          border: 1px solid rgba(255,255,255,0.03); 
          text-align: center; 
          cursor: pointer; 
          transition: all 0.2s; 
        }
        .stat-card:hover { background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.2); }
        .stat-card:active { transform: scale(0.96); }
        .stat-label { font-size: 0.55rem; font-weight: 800; color: var(--secondary-text); text-transform: uppercase; margin-bottom: 2px; display: block; opacity: 0.7; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; }
        .stat-value { font-size: 0.9rem; font-weight: 800; color: white; line-height: 1.1; pointer-events: none; }

        .section-header { font-size: 0.8rem; font-weight: 900; color: #4dabf5; margin: 12px 0 6px; letter-spacing: 0.05em; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 3px; }
        .timeline-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap: 4px; margin-bottom: 6px; }
        
        .hour-bar {
          border-radius: 8px;
          padding: 0;
          aspect-ratio: 1 / 1;
          cursor: pointer;
          position: relative;
          background: transparent;
        }
        .bar-content {
          padding: 2px 1px;
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
        .hour-bar.active .bar-content {
          border-width: 2px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }

        .hour-bar.manual-glow .bar-content {
          box-shadow: inset 0 0 8px rgba(255, 255, 255, 0.4);
        }

        .manual-indicator {
          position: absolute;
          top: 4px;
          right: 4px;
          color: white;
          --mdc-icon-size: 14px;
          background: rgba(0,0,0,0.3);
          border-radius: 50%;
          padding: 2px;
        }
        .h-icon { --mdc-icon-size: 18px; margin-top: 5px; margin-bottom: 1px; }
        .h-time { font-size: 0.85rem; font-weight: 900; color: white; line-height: 1; }
        .h-prices { display: flex; gap: 4px; margin: 2px 0; }
        .price-buy { font-size: 0.6rem; font-weight: 800; color: #90caf9; }
        .price-sell { font-size: 0.6rem; font-weight: 800; color: #a5d6a7; }
        .h-mode { font-size: 0.55rem; font-weight: 800; text-align: center; line-height: 1; margin-top: 2px; text-transform: uppercase; letter-spacing: 0.02em; }
        .h-soc-top-left {
          position: absolute;
          top: 2px;
          left: 4px;
          display: flex;
          align-items: center;
          gap: 1.5px;
          line-height: 1;
          pointer-events: none;
        }
        .h-soc-top-left ha-icon {
          --mdc-icon-size: 11px;
          margin-bottom: 0.5px;
        }
        .h-soc-percent {
          font-size: 0.55rem;
          font-weight: 900;
          letter-spacing: -0.02em;
        }
        .h-forecasts { display: flex; gap: 4px; margin-top: 2px; opacity: 0.7; }
        .h-f-item { display: flex; align-items: center; gap: 2px; font-size: 0.55rem; font-weight: 700; }
        .h-f-item ha-icon { --mdc-icon-size: 10px; }
        .f-gen { color: #ffeb3b; }
        .f-load { color: #f44336; }


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
          background: rgba(255, 255, 255, 0.03);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 24px;
          padding: 20px;
          margin-bottom: 20px;
          display: flex;
          flex-direction: column;
          gap: 14px;
          box-shadow: inset 0 1px 1px rgba(255,255,255,0.1), 0 8px 32px rgba(0,0,0,0.2);
        }
        .info-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-size: 0.95rem;
          color: rgba(255, 255, 255, 0.7);
          padding-bottom: 10px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        }
        .info-row:nth-last-child(2) {
          border-bottom: none;
          padding-bottom: 0;
        }
        .info-label {
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 0.75rem;
          color: rgba(255, 255, 255, 0.45);
          font-weight: 800;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .info-icon {
          --mdc-icon-size: 18px;
          color: #90caf9;
        }
        .info-icon.solar-color { color: #ffe082; }
        .info-icon.power-color { color: #f48fb1; }
        .info-icon.battery-color { color: #a5d6a7; }
        
        .info-value {
          color: #fff;
          font-family: 'Roboto Mono', monospace;
          font-size: 0.95rem;
          font-weight: 600;
        }
        .color-buy { color: #ff6b6b; font-weight: 700; }
        .color-sell { color: #66bb6a; font-weight: 700; }
        .color-gen { color: #ffe082; font-weight: 700; }
        .color-load { color: #ff6b6b; font-weight: 700; }
        .divider { color: rgba(255, 255, 255, 0.25); font-weight: 300; }
        .unit-text { color: rgba(255, 255, 255, 0.45); font-size: 0.85rem; font-weight: 400; }
        
        .soc-badge {
          background: rgba(76, 175, 80, 0.15);
          border: 1px solid rgba(76, 175, 80, 0.3);
          padding: 4px 10px;
          border-radius: 12px;
          box-shadow: 0 0 10px rgba(76, 175, 80, 0.15);
        }
        .soc-badge b {
          color: #81c784;
          font-family: 'Roboto Mono', monospace;
          font-size: 0.95rem;
          font-weight: 700;
        }
        
        .reason-box {
          background: rgba(255, 255, 255, 0.02);
          border-left: 3px solid #03a9f4;
          border-radius: 6px;
          padding: 10px 12px;
          margin-top: 4px;
          display: flex;
          align-items: flex-start;
          gap: 10px;
          font-size: 0.8rem;
          color: rgba(255, 255, 255, 0.65);
          line-height: 1.35;
        }
        .reason-icon {
          --mdc-icon-size: 16px;
          color: #03a9f4;
          flex-shrink: 0;
          margin-top: 1px;
        }
        #info-reason {
          font-style: italic;
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
        .version-tag { position: absolute; bottom: 4px; right: 8px; font-size: 0.5rem; opacity: 0.3; color: var(--secondary-text); pointer-events: none; }

        .tab-container {
          display: flex;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 16px;
          padding: 4px;
          margin: 16px 0;
          gap: 4px;
          position: relative;
          backdrop-filter: blur(10px);
        }
        .tab-btn {
          flex: 1;
          height: 40px;
          border-radius: 12px;
          font-size: 0.75rem;
          font-weight: 800;
          cursor: pointer;
          transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          color: rgba(255,255,255,0.6);
          border: none;
          background: transparent;
        }
        .tab-btn:hover {
          color: white;
          background: rgba(255,255,255,0.02);
        }
        .tab-btn.selected {
          color: white;
          background: rgba(255,255,255,0.08);
          border: 1px solid rgba(255,255,255,0.1);
        }
        .tab-btn.selected.heur-tab {
          border-color: rgba(3, 169, 244, 0.4);
          background: rgba(3, 169, 244, 0.15);
          color: #03a9f4;
          box-shadow: 0 4px 15px rgba(3, 169, 244, 0.1);
        }
        .tab-btn.selected.dp-tab {
          border-color: rgba(76, 175, 80, 0.4);
          background: rgba(76, 175, 80, 0.15);
          color: #4caf50;
          box-shadow: 0 4px 15px rgba(76, 175, 80, 0.1);
        }
        .active-dot {
          display: inline-flex;
          align-items: center;
          font-size: 0.55rem;
          font-weight: 900;
          padding: 2px 6px;
          border-radius: 6px;
          background: rgba(76, 175, 80, 0.1);
          color: #4caf50;
          animation: pulse 2s infinite;
          letter-spacing: 0.05em;
          border: 1px solid rgba(76, 175, 80, 0.2);
        }
        .active-dot.heur-active {
          background: rgba(3, 169, 244, 0.1);
          color: #03a9f4;
          border-color: rgba(3, 169, 244, 0.2);
        }
        @keyframes pulse {
          0% { opacity: 0.6; }
          50% { opacity: 1; }
          100% { opacity: 0.6; }
        }
      </style>
      <ha-card>
        <div class="header">
          <div class="title">Energy Management</div>
          <div id="status-badge" class="status-badge">AI Operational</div>
        </div>

        <div class="stats-panel">
          <div class="hero-row">
            <div id="soc-hero" class="hero-badge" onclick="this.getRootNode().host._handleMoreInfo()">
              <span id="soc-val" class="hero-val">--</span>
              <span class="hero-label">Battery SOC</span>
            </div>
            <div id="profit-hero" class="hero-badge" onclick="this.getRootNode().host._handleMoreInfo(this.getAttribute('data-entity'))">
              <span id="profit-val" class="hero-val">--</span>
              <span id="profit-label" class="hero-label">Today Profit</span>
            </div>
          </div>

          <div class="stats-grid" id="stats-container">
            <div class="stat-card" onclick="this.getRootNode().host._handleMoreInfo()">
              <span class="stat-label">Morning Projection</span>
              <div class="stat-value" id="proj-morning">-</div>
            </div>
            <!-- Extra indicators will be injected here -->
          </div>
        </div>

        <div class="tab-container">
          <button id="tab-heuristic" class="tab-btn heur-tab" onclick="this.getRootNode().host._switchTab('heuristic')">
            <ha-icon icon="mdi:console"></ha-icon>
            <span>Эвристика</span>
            <span id="badge-heur" class="active-dot heur-active" style="display:none;">● ACTIVE</span>
          </button>
          <button id="tab-dp" class="tab-btn dp-tab" onclick="this.getRootNode().host._switchTab('dp')">
            <ha-icon icon="mdi:brain"></ha-icon>
            <span>DP Оптимизатор</span>
            <span id="badge-dp" class="active-dot" style="display:none;">● ACTIVE</span>
          </button>
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
                <div class="info-row">
                  <span class="info-label">
                    <ha-icon icon="mdi:swap-horizontal" class="info-icon"></ha-icon>
                    <span>Buy / Sell</span>
                  </span>
                  <b class="info-value">
                    <span id="info-buy" class="color-buy">-</span>
                    <span class="divider"> / </span>
                    <span id="info-sell" class="color-sell">-</span>
                    <span id="info-currency" class="unit-text"></span>
                  </b>
                </div>
                <div class="info-row">
                  <span class="info-label">
                    <ha-icon icon="mdi:lightning-bolt" class="info-icon solar-color"></ha-icon>
                    <span>Gen / Load</span>
                  </span>
                  <b class="info-value">
                    <span id="info-gen" class="color-gen">-</span>
                    <span class="divider"> / </span>
                    <span id="info-load" class="color-load">-</span>
                    <span class="unit-text"> kW</span>
                  </b>
                </div>
                <div class="info-row" id="info-avg-row">
                  <span class="info-label">
                    <ha-icon icon="mdi:clock-outline" class="info-icon solar-color"></ha-icon>
                    <span>Avg 5M (Gen/Load)</span>
                  </span>
                  <b class="info-value">
                    <span id="info-avg-gen" class="color-gen">-</span>
                    <span class="divider"> / </span>
                    <span id="info-avg-load" class="color-load">-</span>
                    <span class="unit-text"> kW</span>
                  </b>
                </div>
                <div class="info-row" id="info-power-row">
                  <span class="info-label">
                    <ha-icon icon="mdi:flash-outline" class="info-icon power-color"></ha-icon>
                    <span>Power / Amps</span>
                  </span>
                  <b id="info-power" class="info-value">-</b>
                </div>
                <div class="info-row">
                  <span class="info-label">
                    <ha-icon icon="mdi:battery-80" class="info-icon battery-color"></ha-icon>
                    <span>SOC Forecast</span>
                  </span>
                  <span class="soc-badge"><b id="info-forecast-soc">-</b></span>
                </div>
                <div class="reason-box">
                  <ha-icon icon="mdi:information-outline" class="reason-icon"></ha-icon>
                  <span id="info-reason">-</span>
                </div>
              </div>
              <div class="form-group">
                <span class="form-label">Mode Override</span>
                <select id="modal-mode" onchange="this.getRootNode().host._toggleSocVisibility()">
                  <option value="buy">Charge</option>
                  <option value="sale_pv_bat">Discharge</option>
                  <option value="sale_pv_no_bat">Export PV</option>
                  <option value="no_pv_sale_no_bat">Wait</option>
                  <option value="stop_sale">Stop Sale</option>
                  <option value="sale_pv">Normal</option>
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
        <div id="v-tag" class="version-tag">v12.1.28</div>
      </ha-card>
    `;
    this._initialized = true;
    this._updateExtraIndicators();
  }

  _updateSocLabel(val) {
    this.shadowRoot.getElementById('modal-soc-label').innerText = val;
  }

  _openModal(timestamp, currentMode) {
    const attrs = this._hass.states[this._config.entity].attributes;
    let data = attrs.hourly_data || {};
    if (this._activeTab === 'heuristic' && attrs.heuristic_hourly_data && Object.keys(attrs.heuristic_hourly_data).length > 0) {
      data = attrs.heuristic_hourly_data;
    } else if (this._activeTab === 'dp' && attrs.dp_hourly_data && Object.keys(attrs.dp_hourly_data).length > 0) {
      data = attrs.dp_hourly_data;
    }
    const hourData = data[timestamp] || {};
    const currentSocLimit = hourData.soc_limit !== undefined ? hourData.soc_limit : (hourData.soc !== undefined ? hourData.soc : 100);

    this._editingTimestamp = timestamp;
    this._currentHourData = hourData;
    this.shadowRoot.getElementById('modal-title').innerText = timestamp;
    this.shadowRoot.getElementById('modal-mode').value = currentMode === 'ai' ? 'ai' : currentMode;

    const socSlider = this.shadowRoot.getElementById('modal-soc');
    if (socSlider) {
      socSlider.value = currentSocLimit;
      this._updateSocLabel(currentSocLimit);
    }

    // Fill Market Info (v12.0)
    const currency = this._hass.states[this._config.entity].attributes.unit_of_measurement || '';

    const buyEl = this.shadowRoot.getElementById('info-buy');
    const sellEl = this.shadowRoot.getElementById('info-sell');
    const currEl = this.shadowRoot.getElementById('info-currency');
    if (buyEl) buyEl.innerText = hourData.buy_price !== undefined ? hourData.buy_price : '0';
    if (sellEl) sellEl.innerText = hourData.sell_price !== undefined ? hourData.sell_price : '0';
    if (currEl) currEl.innerText = ` ${currency}`;

    const genEl = this.shadowRoot.getElementById('info-gen');
    const loadEl = this.shadowRoot.getElementById('info-load');
    if (genEl) genEl.innerText = hourData.gen !== undefined ? hourData.gen : '0';
    if (loadEl) loadEl.innerText = hourData.load !== undefined ? hourData.load : '0';

    const avgGenEl = this.shadowRoot.getElementById('info-avg-gen');
    const avgLoadEl = this.shadowRoot.getElementById('info-avg-load');
    const avgRow = this.shadowRoot.getElementById('info-avg-row');
    if (hourData.avg_gen !== undefined && hourData.avg_load !== undefined) {
      if (avgRow) avgRow.style.display = 'flex';
      if (avgGenEl) avgGenEl.innerText = hourData.avg_gen.toFixed(2);
      if (avgLoadEl) avgLoadEl.innerText = hourData.avg_load.toFixed(2);
    } else {
      if (avgRow) avgRow.style.display = 'none';
    }

    this.shadowRoot.getElementById('info-power').innerText = `${hourData.power || 0} kW / ${hourData.amps || 0} A`;
    this.shadowRoot.getElementById('info-reason').innerText = hourData.reason || 'Standard AI decision';

    // v12.0.38: Explicitly show Forecast vs Target
    const forecastEl = this.shadowRoot.getElementById('info-forecast-soc');
    if (forecastEl) {
      const displaySoc = (this._activeTab === 'dp' && hourData.soc_limit !== undefined) ? hourData.soc_limit : hourData.soc;
      forecastEl.innerText = `${displaySoc !== undefined ? displaySoc.toFixed(1) : '--'}%`;
    }

    this._toggleSocVisibility();
    this.shadowRoot.getElementById('modal').classList.add('open');
  }

  _toggleSocVisibility() {
    const mode = this.shadowRoot.getElementById('modal-mode').value;

    let resolvedMode = mode;
    if (mode === 'ai' && this._currentHourData) {
      resolvedMode = this._currentHourData.mode;
    }

    const socGroup = this.shadowRoot.getElementById('soc-group');
    if (socGroup) {
      // STRICT: Show ONLY for Buy and Sale_PV_BAT. Hide for AI and everything else.
      const isVisible = (mode === 'buy' || mode === 'sale_pv_bat');
      socGroup.style.display = isVisible ? 'flex' : 'none';
    }

    const powerRow = this.shadowRoot.getElementById('info-power-row');
    if (powerRow) {
      const showPower = (resolvedMode === 'buy' || resolvedMode === 'sale_pv_bat');
      powerRow.style.display = showPower ? 'flex' : 'none';
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

  _handleMoreInfo(entityId) {
    const target = entityId || this._config.entity;
    const event = new CustomEvent('hass-more-info', {
      detail: { entityId: target },
      bubbles: true,
      composed: true
    });
    this.dispatchEvent(event);
  }

  _getBatteryColor(soc) {
    if (soc < 20) return '#f44336'; // Red
    if (soc < 50) return '#ff9800'; // Orange
    if (soc < 80) return '#ffeb3b'; // Yellow
    return '#4caf50'; // Green
  }

  _updateUI() {
    const entityId = this._config.entity || 'sensor.energy_management';
    const stateObj = this._hass.states[entityId];
    if (!stateObj) return;

    const attrs = stateObj.attributes;
    const soc = parseFloat(attrs.battery_soc) || 0;
    const bms = attrs.bms_status || {};

    // Tab switching and active indicators
    const useDP = attrs.use_dp || false;
    if (!this._activeTab) {
      this._activeTab = useDP ? 'dp' : 'heuristic';
    }

    // Toggle ● ACTIVE badge visibility
    const badgeHeur = this.shadowRoot.getElementById('badge-heur');
    const badgeDP = this.shadowRoot.getElementById('badge-dp');
    if (badgeHeur) badgeHeur.style.display = !useDP ? 'inline-flex' : 'none';
    if (badgeDP) badgeDP.style.display = useDP ? 'inline-flex' : 'none';

    // Highlight selected tab button
    const tabHeur = this.shadowRoot.getElementById('tab-heuristic');
    const tabDP = this.shadowRoot.getElementById('tab-dp');
    if (tabHeur) {
      if (this._activeTab === 'heuristic') tabHeur.classList.add('selected');
      else tabHeur.classList.remove('selected');
    }
    if (tabDP) {
      if (this._activeTab === 'dp') tabDP.classList.add('selected');
      else tabDP.classList.remove('selected');
    }

    // Select target timeline data
    let hourlyData = attrs.hourly_data || {};
    if (this._activeTab === 'heuristic' && attrs.heuristic_hourly_data && Object.keys(attrs.heuristic_hourly_data).length > 0) {
      hourlyData = attrs.heuristic_hourly_data;
    } else if (this._activeTab === 'dp' && attrs.dp_hourly_data && Object.keys(attrs.dp_hourly_data).length > 0) {
      hourlyData = attrs.dp_hourly_data;
    }

    // Update Hero Badges
    const socColor = this._getBatteryColor(soc);
    const socHero = this.shadowRoot.getElementById('soc-hero');
    if (socHero) {
      socHero.style.borderColor = socColor;
      socHero.style.boxShadow = `0 6px 20px ${socColor}22`;
    }
    const socVal = this.shadowRoot.getElementById('soc-val');
    if (socVal) {
      socVal.innerText = Math.round(soc) + '%';
      socVal.style.color = socColor;
    }

    // Profit Badge
    const profitEntity = this._config.profit_entity;
    const profitHero = this.shadowRoot.getElementById('profit-hero');
    if (profitEntity && this._hass.states[profitEntity]) {
      const pState = this._hass.states[profitEntity];
      const pValRaw = parseFloat(pState.state) || 0;
      const pColor = pValRaw >= 0 ? '#4caf50' : '#f44336';

      profitHero.style.display = 'flex';
      profitHero.setAttribute('data-entity', profitEntity);
      profitHero.style.borderColor = pColor;
      profitHero.style.boxShadow = `0 6px 20px ${pColor}22`;

      const pLabel = this.shadowRoot.getElementById('profit-label');
      pLabel.innerText = this._config.profit_label || 'Today Profit';

      const pValEl = this.shadowRoot.getElementById('profit-val');
      pValEl.innerText = pValRaw.toFixed(2) + (pState.attributes.unit_of_measurement || '');
      pValEl.style.color = pColor;
    } else if (profitHero) {
      profitHero.style.display = 'none';
      if (socHero) socHero.style.gridColumn = 'span 2';
    }

    const vTag = this.shadowRoot.getElementById('v-tag');
    if (vTag) vTag.innerText = attrs.strategy_version || 'v12.1.0';

    const projM = this.shadowRoot.getElementById('proj-morning');
    if (projM) projM.innerText = (parseFloat(attrs.morning_soc_projected) || 0).toFixed(1) + '%';

    // v11.9.405: Dynamic Extra Indicators from Config
    this._updateExtraIndicators();

    const badge = this.shadowRoot.getElementById('status-badge');
    if (badge) {
      const modeLabel = MODE_LABELS[stateObj.state] || stateObj.state.toUpperCase();
      badge.innerText = modeLabel;
      const color = MODE_COLORS[stateObj.state] || MODE_COLORS.default;
      badge.style.color = color;
      badge.style.borderColor = color;
    }



    this._renderTimeline(hourlyData);
  }

  _updateExtraIndicators() {
    const container = this.shadowRoot.getElementById('stats-container');
    if (!container || !this._hass) return;

    const extras = this._config.extra_indicators || [];
    const currentEntityIds = extras.map(item => item.entity);

    // 1. Remove cards that are no longer in config
    Array.from(container.querySelectorAll('.stat-card[data-entity]')).forEach(card => {
      if (!currentEntityIds.includes(card.getAttribute('data-entity'))) {
        card.remove();
      }
    });

    // 2. Add or Update cards
    extras.forEach(item => {
      const stateObj = this._hass.states[item.entity];
      if (!stateObj) return;

      let card = container.querySelector(`.stat-card[data-entity="${item.entity}"]`);
      if (!card) {
        card = document.createElement('div');
        card.className = 'stat-card';
        card.setAttribute('data-entity', item.entity);
        card.onclick = () => this._handleMoreInfo(item.entity);
        card.innerHTML = `<span class="stat-label"></span><div class="stat-value"></div>`;
        container.appendChild(card);
      }

      const label = card.querySelector('.stat-label');
      const value = card.querySelector('.stat-value');

      const newLabel = item.name || stateObj.attributes.friendly_name || 'Sensor';
      const newVal = `${stateObj.state} ${stateObj.attributes.unit_of_measurement || ''}`;

      if (label.innerText !== newLabel) label.innerText = newLabel;
      if (value.innerText !== newVal) value.innerText = newVal;
    });
  }

  _renderTimeline(data) {
    if (data && Object.keys(data).length > 0) {
      console.log("[Energy Management] Rendering timeline with data:", data);
    }
    const container = this.shadowRoot.getElementById('timeline-container');
    if (!container) return;

    const entityId = this._config.entity || 'sensor.energy_management';
    const stateObj = this._hass.states[entityId];
    const attrs = (stateObj && stateObj.attributes) ? stateObj.attributes : {};
    const serverToday = attrs.server_today;

    const now = new Date();
    // v11.9.696: Trust server's definition of 'Today' to avoid timezone desync
    const todayStr = serverToday || (now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0'));

    let tomorrowStr = '';
    try {
      const parts = todayStr.split('-');
      const todayDate = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
      const tomorrowDate = new Date(todayDate);
      tomorrowDate.setDate(tomorrowDate.getDate() + 1);
      tomorrowStr = tomorrowDate.getFullYear() + '-' + String(tomorrowDate.getMonth() + 1).padStart(2, '0') + '-' + String(tomorrowDate.getDate()).padStart(2, '0');
    } catch (e) {
      const tomorrowDate = new Date();
      tomorrowDate.setDate(tomorrowDate.getDate() + 1);
      tomorrowStr = tomorrowDate.getFullYear() + '-' + String(tomorrowDate.getMonth() + 1).padStart(2, '0') + '-' + String(tomorrowDate.getDate()).padStart(2, '0');
    }

    const currentHour = now.getHours();
    const sortedKeys = Object.keys(data).sort();
    if (sortedKeys.length === 0) return;

    const currentHourStr = `${todayStr} ${currentHour < 10 ? '0' + currentHour : currentHour}:00`;

    // Filter to show from NOW until the end of Tomorrow (Today + Tomorrow only)
    const windowKeys = sortedKeys.filter(k => {
      if (typeof k !== 'string') return false;
      return k >= currentHourStr && (k.includes(todayStr) || k.includes(tomorrowStr));
    });

    const hexToRgba = (hex, alpha) => {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    };

    // Smart DOM Update Logic
    // Ensure we display all hours that have a planned mode (so we don't accidentally hide zero/negative prices)
    let filteredKeys = windowKeys.filter(key => {
      const h = data[key];
      return (h.mode !== undefined || h.is_manual);
    });

    // Check if tomorrow's prices are available
    const tomorrowKeys = filteredKeys.filter(k => typeof k === 'string' && k.includes(tomorrowStr));
    const hasTomorrowPrices = tomorrowKeys.some(key => {
      const h = data[key];
      return (h.buy_price && h.buy_price !== 0) || (h.sell_price && h.sell_price !== 0);
    });

    if (tomorrowKeys.length > 0 && !hasTomorrowPrices) {
      filteredKeys = filteredKeys.filter(k => typeof k === 'string' && k.includes(todayStr));
    }

    const currentKeysStr = filteredKeys.join(',');
    if (container._lastKeys !== currentKeysStr) {
      // Structure changed -> Full rebuild
      let html = '';
      let currentDayLabel = '';
      filteredKeys.forEach((key, idx) => {
        const isTomorrow = typeof key === 'string' && key.includes(tomorrowStr);
        const label = isTomorrow ? 'TOMORROW' : 'TODAY';
        const hourData = data[key];

        if (label !== currentDayLabel) {
          if (currentDayLabel !== '') html += '</div>';
          html += `<div class="section-header">${label}</div><div class="timeline-grid">`;
          currentDayLabel = label;
        }

        const modeColor = MODE_COLORS[hourData.mode] || MODE_COLORS.default;
        const bgColor = hexToRgba(modeColor, 0.1);
        const isManual = hourData.is_manual;

        const displaySoc = (this._activeTab === 'dp' && hourData.soc_limit !== undefined) ? hourData.soc_limit : hourData.soc;
        const socInfo = getSocInfo(displaySoc);

        html += `
          <div class="hour-bar ${idx === 0 ? 'active' : ''} ${isManual ? 'manual-glow' : ''}" data-ts="${key}" data-mode="${hourData.mode}" id="hb-${key.replace(/[: ]/g, '-')}">
            <div class="bar-content" style="border-color: ${modeColor}; background-color: ${bgColor};">
              <div class="h-soc-top-left" style="color: ${socInfo.color};">
                <ha-icon icon="${socInfo.icon}"></ha-icon>
                <span class="h-soc-percent">${socInfo.percent ? socInfo.percent + '%' : ''}</span>
              </div>
              ${isManual ? `<ha-icon class="manual-indicator" icon="mdi:hand-back-right"></ha-icon>` : ''}
              <ha-icon class="h-icon" style="color:${modeColor}" icon="${MODE_ICONS[hourData.mode] || MODE_ICONS.default}"></ha-icon>
              <span class="h-time">${key.split(' ')[1]}</span>
              <div class="h-prices">
                <span class="price-buy">${(hourData.buy_price ?? 0).toFixed(2)}</span>
                <span class="price-sell">${(hourData.sell_price ?? 0).toFixed(2)}</span>
              </div>
              <div class="h-mode" style="color:${modeColor}">${MODE_LABELS[hourData.mode] || hourData.mode}</div>
            </div>
          </div>
        `;
      });
      if (html !== '') html += '</div>'; // Close last grid
      container.innerHTML = html;
      container._lastKeys = currentKeysStr;

      // Re-bind listeners
      container.querySelectorAll('.hour-bar').forEach(bar => {
        bar.addEventListener('click', () => this._openModal(bar.getAttribute('data-ts'), bar.getAttribute('data-mode')));
      });
    } else {
      // Structure same -> Point update to preserve hover states
      filteredKeys.forEach(key => {
        const hourData = data[key];
        const bar = container.querySelector(`#hb-${key.replace(/[: ]/g, '-')}`);
        if (!bar) return;

        const modeColor = MODE_COLORS[hourData.mode] || MODE_COLORS.default;
        const isManual = hourData.is_manual;
        const content = bar.querySelector('.bar-content');
        if (isManual) {
          bar.classList.add('manual-glow');
          if (!bar.querySelector('.manual-indicator')) {
            const ind = document.createElement('ha-icon');
            ind.className = 'manual-indicator';
            ind.icon = 'mdi:hand-back-right';
            content.appendChild(ind);
          }
        } else {
          bar.classList.remove('manual-glow');
          const ind = bar.querySelector('.manual-indicator');
          if (ind) ind.remove();
        }

        const icon = bar.querySelector('.h-icon');
        const modeLabel = bar.querySelector('.h-mode');
        const buyPrice = bar.querySelector('.price-buy');
        const sellPrice = bar.querySelector('.price-sell');
        const priceContainer = bar.querySelector('.h-prices');
        const socContainer = bar.querySelector('.h-soc-top-left');

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

        if (socContainer) {
          const displaySoc = (this._activeTab === 'dp' && hourData.soc_limit !== undefined) ? hourData.soc_limit : hourData.soc;
          const socInfo = getSocInfo(displaySoc);
          socContainer.style.color = socInfo.color;
          const socIcon = socContainer.querySelector('ha-icon');
          if (socIcon) socIcon.icon = socInfo.icon;
          const socPercent = socContainer.querySelector('.h-soc-percent');
          if (socPercent) socPercent.innerText = socInfo.percent ? `${socInfo.percent}%` : '';
        }
        if (priceContainer) {
          priceContainer.style.display = 'flex';
          if (buyPrice) buyPrice.innerText = (hourData.buy_price ?? 0).toFixed(2);
          if (sellPrice) sellPrice.innerText = (hourData.sell_price ?? 0).toFixed(2);
        }
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
