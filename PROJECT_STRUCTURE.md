# Проектная структура: Energy Management Integration

Этот документ содержит полное техническое описание архитектуры проекта, назначение каждого файла и подробную документацию абсолютно всех имеющихся в них функций, методов и процедур.

---

## 1. Общее описание
Интеграция для Home Assistant для оптимизации энергопотребления (Солнечные панели + АКБ). Арбитраж цен, защита аккумулятора, прогнозирование.

---

## 2. Вспомогательные файлы

### [const.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/const.py)
**Назначение:** Глобальные константы и ключи настроек.
- Функций нет.

### [utils.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/utils.py)
**Назначение:** Общие утилиты.
- `get_kwh_val(state) -> float`: Извлечение kWh из состояния.
- `normalize_float(val, default=0.0) -> float`: Безопасное приведение к числу.
- `get_price_from_store(store, date_str, hour) -> float`: Получение цены.
- `round_f(val, precision=3) -> float`: Округление с защитой от None.

---

## 3. Ядро логики

### [strategy_base.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/strategy_base.py)
**Назначение:** Базовый движок симуляций `StrategyEngine`.

#### Методы:
- `__init__(self, manager)`: Инициализация.
- `clear_cache()`: Очистка кэша.
- `get_cc_cv_ratio(soc) -> float`: Ограничение мощности по BMS.
- `_format_h(h_abs) -> str`: Форматирование часа.
- `_group_contiguous(hours) -> list`: Группировка часов в периоды.
- `get_battery_degradation_cost() -> float`: Стоимость износа 1 кВт·ч.
- `get_efficiency_coefficient() -> float`: КПД системы.
- `get_survival_floor(start_h_abs, end_h_abs) -> float`: Порог SOC для выживания до утра.
- `get_gatekeeper_floor(h_abs, end_h_abs) -> float`: Динамическая защита (Gatekeeper).
- `_get_sunrise_baseline_soc(...)`: Базовый уровень SOC на рассвете.
- `_calculate_sunrise_surplus(...)`: Расчет излишков на утро.
- `_calc_immediate_safety_floor(...)`: Мгновенный порог безопасности.
- `get_hourly_accuracy_coeff(hour) -> float`: Коэффициент точности.
- `get_gen_forecast_coefficient(...) -> float`: Масштабирование прогноза солнца.
- `run_investment_simulation(...)`: Долгосрочная симуляция окупаемости.
- `get_budget_and_permissions(...)`: Сбор данных для стратегий.
- `_get_soc_from_log(log, key, default) -> float`: Извлечение SOC из лога.
- `run_soc_simulation(...) -> (final_soc, log, summary)`: **Почасовое моделирование баланса энергии.**
- `get_market_strategy(mode="buy")`: Заглушка.
- `_get_arbitrage_info(...)`: Оценка выгоды арбитража.

### [strategy_buy.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/strategy_buy.py)
**Назначение:** Стратегия покупки (Зарядка).
- `get_market_strategy(mode="buy")`: Алгоритм планирования покупки: поиск отрицательных цен, дешевых окон, арбитражных сделок и обеспечение ночного выживания через "мостики" заряда.

### [strategy_sell.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/strategy_sell.py)
**Назначение:** Стратегия продажи (Разрядка).
- `get_strategy_epochs(target_hours, prices_today, prices_tomorrow)`: Группировка окон продажи.
- `get_market_strategy(mode="sell")`: Алгоритм продажи: поиск пиков цен, расчет излишков, итеративная подгонка бюджета через симуляции дефицита.
- `_group_contiguous(hours)`: Группировка (переопределено).
- `_get_soc_from_log(log, key, default)`: Получение SOC.

### [strategy_dp.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/strategy_dp.py)
**Назначение:** Планирование через Динамическое Программирование.
- `get_dp_advice(data_snapshot=None)`: Построение оптимального графа состояний АКБ на 48 часов.
- `_calc_survival_beyond_horizon(...)`: Оценка энергии после горизонта.
- `_get_smart_gen_forecast(...)`: Комбинированный прогноз.
- `_ensure_dict(data)`, `_get_prices(key)`, `_get_deg_cost(cap)`: Вспомогательные методы.

---

## 4. Менеджер и Сенсоры

### [sensor.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/sensor.py)

#### Класс `EnergyProfileManager` (Сердце системы):
- `async_load()`: Загрузка данных.
- `async_start()` / `async_stop()`: Управление таймерами.
- `_notify_update()`: Уведомление сенсоров.
- `get_average_profile(type, days, day_type)`: Историческое усреднение.
- `get_predicted_profile(type)`: Прогноз (История + Solcast).
- `get_todays_profile(type)`: Текущие данные за сегодня.
- `get_setting(key, default)`: Чтение настроек.
- `get_price(mode, date, hour)`: Запрос цены.
- `_is_currently_pulling_power(sensor_id)`: Детекция активного потребления.
- `get_sensor_float(entity_id)`: Чтение сенсора HA.
- `get_battery_state()`: Состояние АКБ.
- `get_forecast_value(sensors)`: Сумма прогноза.
- `get_forecast_hourly_distribution(...)`: Почасовой прогноз.
- `get_battery_charge_limit_kw(soc)`: Ограничение BMS.
- `_update_bms_learned_profile(now)`: Обучение модели АКБ.

#### Классы сенсоров (Все реализуют `native_value` и `extra_state_attributes`):
- `UniversalPriceSensor`: Текущая цена.
- `ProfileAveragedSensor`: Средний профиль.
- `BMSLearnedProfileSensor`: Обученная модель АКБ.
- `BatteryEndOfDaySOCSensor`: Прогноз SOC на 00:00.
- `ConsumptionDeviationSensor`: Отклонение от нормы.
- `InverterOperationModeSensor`: Выбор режима (`_get_mode_at`).
- `InstantPowerAveragedSensor`: Средняя мощность.
- `LiveHourlySensor`: Текущий час.
- `TodayProfileSensor`: Профиль за сегодня.
- `EnergyBudgetSensor`: Бюджет энергии.
- `MarketStrategySensor`: Описание плана.
- `SavingsSensor`: Экономия.
- `EnergyBalanceSensor`: Баланс.
- `AnomalyDetectionSensor`: Детектор аномалий.
- `PaybackSensor`: Окупаемость.
- `BatteryDegradationSensor`: Износ.
- `SolarWasteSensor`: Потерянное солнце.
- `BatteryAutonomySensor`: Автономия.
- `GridBalanceSensor`: Баланс сети.
- `PotentialExportTodaySensor`: Потенциал экспорта.
- `EnergyDPAdviceSensor`: Советы DP.

---

## 5. Интеграция в HA

### [__init__.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/__init__.py)
- `async_setup`, `async_setup_entry`, `async_unload_entry`.
- Сервисы: `reset_data`, `export_data`, `import_data`, `reset_bms_profile`, `force_buy`, `stop_sale`, `ai_mode`, `set_hourly_override`.
- `_async_register_ws_version`, `_async_register_card`.

### [config_flow.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/config_flow.py)
- `ConfigFlow`: Настройка интеграции.
- `OptionsFlowHandler`: Изменение настроек.

### [binary_sensor.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/binary_sensor.py)
- `EnergyArbitragePossibleSensor`.

### [number.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/number.py)
- `EnergyManagementNumber`: Числовые настройки в UI.

### [switch.py](file:///g:/systemair/energy_mamagment/custom_components/energy_management/switch.py)
- `EnergyManagementSwitch`: Переключатели в UI.
